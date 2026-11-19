<?php
// Pure merge engine: applies pushed cue events to a show's state. No I/O, so it is unit-testable.
//
// Event identity is the LoRa (origin, session, seq), so redundant pushers and retries are idempotent.
// Event time is derived from the server clock minus the pusher's reported age, so retries and wrong
// pusher clocks don't skew countdowns. Each zone is last-writer-wins on event time, so late
// deliveries can never overwrite newer state. Effects (flash/sound) are only published while fresh.
declare(strict_types=1);

const LORAQ_COLOURS = ['W', 'R', 'G', 'Y', 'B', 'O'];
// effect id => [flash, sound]; mirrors EFFECTS in loraq_protocol.py
const LORAQ_EFFECTS = [
    0 => [false, null], 1 => [true, null], 2 => [true, 'buzzer'], 3 => [false, 'buzzer'],
    4 => [true, 'beep'], 5 => [false, 'beep'], 6 => [true, 'double-beep'], 7 => [false, 'double-beep'],
];
const LORAQ_MAX_TIMER_S = 5999;
const LORAQ_MAX_TEXT_BYTES = 2000;
const LORAQ_MAX_RECORDS = 8;
const LORAQ_MAX_AGE_MS = 7 * 86400 * 1000;
const LORAQ_EFFECT_FRESH_MS = 3000;
const LORAQ_REFINE_MS = 5000;
const LORAQ_EVENT_TTL_MS = 86400 * 1000;
const LORAQ_MAX_EVENTS_KEPT = 5000;

function loraq_empty_store(): array
{
    return ['version' => 0, 'zones' => ['1' => null, '2' => null], 'events' => [],
            'effect' => null, 'source' => null, 'reporters' => [], 'updated_ms' => 0];
}

function loraq_now_ms(): int
{
    return (int) floor(microtime(true) * 1000);
}

function loraq_is_list(array $value): bool
{
    $index = 0;
    foreach ($value as $key => $_) {
        if ($key !== $index++) {
            return false;
        }
    }
    return true;
}

function loraq_valid_station_id($station): bool
{
    return is_string($station) && preg_match('/^[A-Za-z0-9_-]{1,16}$/D', $station);
}

/** Returns the event key string, or null if origin/session/seq are malformed. */
function loraq_event_key($e): ?string
{
    if (!is_array($e)) {
        return null;
    }
    $origin = $e['origin'] ?? null;
    $session = $e['session'] ?? null;
    $seq = $e['seq'] ?? null;
    if (!loraq_valid_station_id($origin)) {
        return null;
    }
    if (!is_string($session) || !preg_match('/^[0-9A-F]{4}$/D', $session)) {
        return null;
    }
    if (!is_int($seq) || $seq < 1 || $seq > 9999) {
        return null;
    }
    return "$origin|$session|$seq";
}

/** Validates an event with a valid key. Returns the normalised event or an error string. */
function loraq_validate_event(array $e)
{
    $age = $e['age_ms'] ?? null;
    if (!is_int($age) || $age < 0 || $age > LORAQ_MAX_AGE_MS) {
        return 'bad age_ms';
    }
    $records = $e['records'] ?? null;
    if (!is_array($records) || !loraq_is_list($records) || !$records || count($records) > LORAQ_MAX_RECORDS) {
        return 'records must be a list of 1-' . LORAQ_MAX_RECORDS;
    }
    $clean = [];
    foreach ($records as $i => $r) {
        if (!is_array($r)) {
            return "record $i is not an object";
        }
        $op = $r['op'] ?? null;
        $area = $r['area'] ?? null;
        $colour = $r['colour'] ?? 'W';
        $timer = $r['timer'] ?? null;
        $effect = $r['effect_id'] ?? 0;
        $text = $r['text'] ?? '';
        $areas = is_string($op) ? (['D' => ['1', '2'], 'C' => ['0', '1', '2'], 'F' => ['0']][$op] ?? null) : null;
        if ($areas === null) {
            return "record $i: bad op";
        }
        if (!in_array($area, $areas, true)) {
            return "record $i: bad area";
        }
        if (!in_array($colour, LORAQ_COLOURS, true)) {
            return "record $i: bad colour";
        }
        if ($timer !== null && (!is_int($timer) || $timer < 0 || $timer > LORAQ_MAX_TIMER_S)) {
            return "record $i: bad timer";
        }
        if (!is_int($effect) || !isset(LORAQ_EFFECTS[$effect])) {
            return "record $i: bad effect_id";
        }
        if (!is_string($text) || strlen($text) > LORAQ_MAX_TEXT_BYTES || !preg_match('//u', $text)) {
            return "record $i: bad text";
        }
        $clean[] = ['op' => $op, 'area' => $area, 'colour' => $colour,
                    'timer' => $op === 'D' ? $timer : null, 'effect_id' => $op === 'C' ? 0 : $effect,
                    'text' => $op === 'D' ? $text : ''];
    }
    return ['origin' => $e['origin'], 'session' => $e['session'], 'seq' => $e['seq'],
            'age_ms' => $age, 'records' => $clean];
}

function loraq_is_newer(?array $zone, int $t, string $key): bool
{
    return $zone === null || $t > $zone['t'] || ($t === $zone['t'] && strcmp($key, $zone['key']) >= 0);
}

/**
 * Applies one validated event. Returns 'applied', 'duplicate' or 'refined' (a duplicate that moved the
 * event earlier, e.g. a pusher that heard the cue sooner or with less queueing than the first reporter).
 */
function loraq_apply_event(array &$store, array $ev, int $now): string
{
    $key = "{$ev['origin']}|{$ev['session']}|{$ev['seq']}";
    $t = $now - $ev['age_ms'];

    if (isset($store['events'][$key])) {
        $old = $store['events'][$key]['t'];
        if ($t >= $old || $old - $t > LORAQ_REFINE_MS) {
            return 'duplicate';
        }
        $delta = $t - $old;
        $store['events'][$key]['t'] = $t;
        foreach ($store['zones'] as $z => $zone) {
            if ($zone !== null && $zone['key'] === $key) {
                $store['zones'][$z]['t'] += $delta;
                if ($zone['ends_at'] !== null) {
                    $store['zones'][$z]['ends_at'] += $delta;
                }
            }
        }
        if ($store['effect'] !== null && $store['effect']['id'] === $key) {
            $store['effect']['at'] += $delta;
        }
        if ($store['source'] !== null && $store['source']['key'] === $key) {
            $store['source']['at'] += $delta;
        }
        $store['version']++;
        return 'refined';
    }

    $store['events'][$key] = ['t' => $t, 'r' => $now];
    $flash = false;
    $flashColour = 'W';
    $sound = null;
    foreach ($ev['records'] as $rec) {
        if ($rec['op'] === 'F') {
            [$f, $s] = LORAQ_EFFECTS[$rec['effect_id']];
            if ($f) {
                $flash = true;
                $flashColour = $store['zones']['1']['colour'] ?? 'W';
            }
            $sound = $s ?? $sound;
            continue;
        }
        if ($rec['op'] === 'C') {
            foreach ($rec['area'] === '0' ? ['1', '2'] : [$rec['area']] as $z) {
                if (loraq_is_newer($store['zones'][$z], $t, $key)) {
                    $store['zones'][$z] = ['text' => '', 'colour' => 'W', 'timer' => null,
                                           'ends_at' => null, 't' => $t, 'key' => $key];
                    if ($z === '1' && $store['effect'] !== null && $t >= $store['effect']['at']) {
                        $store['effect'] = null;
                    }
                }
            }
            continue;
        }
        $z = $rec['area'];
        if (loraq_is_newer($store['zones'][$z], $t, $key)) {
            $store['zones'][$z] = ['text' => $rec['text'], 'colour' => $rec['colour'], 'timer' => $rec['timer'],
                                   'ends_at' => $rec['timer'] === null ? null : $t + $rec['timer'] * 1000,
                                   't' => $t, 'key' => $key];
        }
        [$f, $s] = LORAQ_EFFECTS[$rec['effect_id']];
        if ($f) {
            $flash = true;
            $flashColour = $rec['colour'];
        }
        $sound = $s ?? $sound;
    }

    if (($flash || $sound !== null) && $now - $t <= LORAQ_EFFECT_FRESH_MS
            && ($store['effect'] === null || $t >= $store['effect']['at'])) {
        $store['effect'] = ['id' => $key, 'at' => $t, 'flash' => $flash,
                            'colour' => $flashColour, 'sound' => $sound];
    }
    if ($store['source'] === null || $t >= $store['source']['at']) {
        $store['source'] = ['station' => $ev['origin'], 'at' => $t, 'key' => $key];
    }
    $store['version']++;
    return 'applied';
}

function loraq_touch_reporter(array &$store, string $reporter, array $body, int $now): void
{
    $int = function ($v) {
        return is_int($v) && $v >= 0 ? min($v, 1000000) : 0;
    };
    $version = $body['app_version'] ?? '';
    // Pushers report their outbox size before sending, which includes the events in this request.
    $events = $body['events'] ?? [];
    $pending = max(0, $int($body['queue_depth'] ?? null) - (is_array($events) ? count($events) : 0));
    $store['reporters'][$reporter] = [
        'last_seen' => $now,
        'queue_depth' => $pending,
        'dropped' => $int($body['dropped'] ?? null),
        'app_version' => is_string($version) && preg_match('/^[A-Za-z0-9._-]{0,16}$/D', $version) ? $version : '',
    ];
    $store['version']++;
}

function loraq_prune(array &$store, int $now): void
{
    $events = array_filter($store['events'], function ($e) use ($now) {
        return $now - $e['r'] <= LORAQ_EVENT_TTL_MS;
    });
    if (count($events) > LORAQ_MAX_EVENTS_KEPT) {
        uasort($events, function ($a, $b) {
            return $b['r'] <=> $a['r'];
        });
        $events = array_slice($events, 0, LORAQ_MAX_EVENTS_KEPT, true);
    }
    $store['events'] = $events;
}

/** Public snapshot for viewers (no dedup history or event keys of zones). */
function loraq_snapshot(array $store): array
{
    $zone = function ($z) {
        return $z === null ? null : ['text' => $z['text'], 'colour' => $z['colour'],
                                     'timer' => $z['timer'], 'ends_at' => $z['ends_at']];
    };
    $reporters = [];
    foreach ($store['reporters'] as $name => $r) {
        $reporters[] = ['name' => (string) $name, 'last_seen' => $r['last_seen'], 'queue_depth' => $r['queue_depth']];
    }
    $source = $store['source'] === null ? null
        : ['station' => $store['source']['station'], 'at' => $store['source']['at']];
    return ['v' => $store['version'],
            'zones' => ['1' => $zone($store['zones']['1']), '2' => $zone($store['zones']['2'])],
            'effect' => $store['effect'], 'source' => $source, 'reporters' => $reporters,
            'updated_ms' => $store['updated_ms']];
}
