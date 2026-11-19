<?php
// Merge engine tests. Run: php web/tests/merge_test.php
declare(strict_types=1);

require __DIR__ . '/../loraq/app/bootstrap.php';

$failures = 0;
$count = 0;

function check(bool $cond, string $name): void
{
    global $failures, $count;
    $count++;
    if (!$cond) {
        $failures++;
        echo "FAIL: $name\n";
    }
}

function ev(string $origin, int $seq, int $age, array $records, string $session = 'AB12'): array
{
    $e = loraq_validate_event(['origin' => $origin, 'session' => $session, 'seq' => $seq,
                               'age_ms' => $age, 'records' => $records]);
    if (is_string($e)) {
        throw new RuntimeException($e);
    }
    return $e;
}

function d(string $area, string $text, ?int $timer = null, string $colour = 'W', int $effect = 0): array
{
    return ['op' => 'D', 'area' => $area, 'colour' => $colour, 'timer' => $timer, 'effect_id' => $effect, 'text' => $text];
}

function c(string $area): array
{
    return ['op' => 'C', 'area' => $area];
}

check(loraq_is_list([1, 2]), 'sequential integer keys are a list');
check(loraq_is_list([]), 'empty array is a list');
check(!loraq_is_list(['first' => 1]), 'associative array is not a list');

$now = 1_000_000_000;

$token = 'sample_shared_token';
$shared = loraq_shared_token(['Train_tech' => $token, 'Marquee' => $token], 'main');
$shows = ['main' => ['push_token' => $shared]];
check(loraq_find_token($token, $shows) === 'main', 'the same token under two legacy labels still works');
check(loraq_shared_token(['Train_tech' => $token], 'main') === $token, 'a single legacy label also works');
check(loraq_shared_token(['Train_tech' => $token, 'Marquee' => 'sample_device_token'], 'main') === null,
    'distinct per-device tokens no longer enable pushes');
$s = loraq_empty_store();
loraq_touch_reporter($s, 'Marquee', [], $now);
loraq_touch_reporter($s, 'Booth-1', [], $now);
loraq_apply_event($s, ev('Train_tech', 1, 0, [d('1', 'Standby')]), $now);
$snap = loraq_snapshot($s);
check(array_column($snap['reporters'], 'name') === ['Marquee', 'Booth-1']
    && $snap['source']['station'] === 'Train_tech', 'shared reporters stay separate from the cue origin');
check(!loraq_valid_station_id(null) && !loraq_valid_station_id('not a station')
    && !loraq_valid_station_id(42), 'shared pushes require a valid Station ID');
check(loraq_find_token('wrong_token', $shows) === null, 'unknown tokens cannot push');
$shows['other'] = ['push_token' => $token];
check(loraq_find_token($token, $shows) === null, 'a reused token cannot select an ambiguous show');

// Basic display with timer: ends_at is anchored to the event time, not receipt time.
$s = loraq_empty_store();
check(loraq_apply_event($s, ev('TX1', 1, 4000, [d('1', 'Standby', 60, 'R')]), $now) === 'applied', 'apply');
check($s['zones']['1']['text'] === 'Standby' && $s['zones']['1']['colour'] === 'R', 'zone 1 set');
check($s['zones']['1']['ends_at'] === $now - 4000 + 60000, 'delayed delivery keeps true countdown');
check($s['source']['station'] === 'TX1', 'source');

// Redundant copy from another pusher: idempotent.
$v = $s['version'];
check(loraq_apply_event($s, ev('TX1', 1, 1000, [d('1', 'Standby', 60, 'R')]), $now + 2000) === 'duplicate', 'duplicate (later)');
check($s['version'] === $v && $s['zones']['1']['ends_at'] === $now + 56000, 'duplicate changes nothing');

// Duplicate that proves the event happened earlier refines the timing.
$s2 = loraq_empty_store();
loraq_apply_event($s2, ev('TX1', 5, 0, [d('2', 'Q5', 30)]), $now);
check(loraq_apply_event($s2, ev('TX1', 5, 1500, [d('2', 'Q5', 30)]), $now) === 'refined', 'refine earlier copy');
check($s2['zones']['2']['ends_at'] === $now - 1500 + 30000, 'refine shifts ends_at');
check(loraq_apply_event($s2, ev('TX1', 5, 60000, [d('2', 'Q5', 30)]), $now) === 'duplicate', 'implausible refinement ignored');

// Out of order: an older cue arriving late must not overwrite a newer one.
$s = loraq_empty_store();
loraq_apply_event($s, ev('TX1', 11, 0, [d('1', 'New')]), $now);
loraq_apply_event($s, ev('TX1', 10, 5000, [d('1', 'Old')]), $now + 100);
check($s['zones']['1']['text'] === 'New', 'late older cue does not win');

// Clear vs display race: a late clear that happened before the display is ignored.
$s = loraq_empty_store();
loraq_apply_event($s, ev('TX1', 21, 0, [d('1', 'Go')]), $now);
loraq_apply_event($s, ev('TX1', 20, 3000, [c('1')]), $now + 10);
check($s['zones']['1']['text'] === 'Go', 'stale clear ignored');
loraq_apply_event($s, ev('TX1', 22, 0, [c('0')]), $now + 20);
check($s['zones']['1']['text'] === '' && $s['zones']['2']['text'] === '', 'clear all');
// Display older than the clear arriving after it is ignored.
loraq_apply_event($s, ev('TX1', 19, 10000, [d('2', 'Ancient')]), $now + 30);
check($s['zones']['2']['text'] === '', 'older display after clear ignored');

// Per-zone independence: different zones from different transmitters.
$s = loraq_empty_store();
loraq_apply_event($s, ev('TXA', 1, 0, [d('1', 'From A')]), $now);
loraq_apply_event($s, ev('TXB', 1, 0, [d('2', 'From B')]), $now + 5);
check($s['zones']['1']['text'] === 'From A' && $s['zones']['2']['text'] === 'From B', 'zones independent');
check($s['source']['station'] === 'TXB', 'source = latest');

// Effects: fresh flash published, stale (retried after outage) not.
$s = loraq_empty_store();
loraq_apply_event($s, ev('TX1', 30, 500, [d('1', 'Flash', null, 'Y', 2)]), $now);
check($s['effect'] !== null && $s['effect']['flash'] && $s['effect']['sound'] === 'buzzer'
    && $s['effect']['colour'] === 'Y', 'fresh effect');
$s = loraq_empty_store();
loraq_apply_event($s, ev('TX1', 31, 10000, [d('1', 'Old flash', null, 'Y', 1)]), $now);
check($s['effect'] === null && $s['zones']['1']['text'] === 'Old flash', 'stale effect skipped, text still applied');

// F record flashes with Area 1 colour; clearing Area 1 stops the flash.
$s = loraq_empty_store();
loraq_apply_event($s, ev('TX1', 40, 0, [d('1', 'Hold', null, 'G')]), $now);
loraq_apply_event($s, ev('TX1', 41, 0, [['op' => 'F', 'area' => '0', 'effect_id' => 5]]), $now + 10);
check($s['effect']['flash'] === false && $s['effect']['sound'] === 'beep', 'F sound only');
loraq_apply_event($s, ev('TX1', 42, 0, [['op' => 'F', 'area' => '0', 'effect_id' => 1]]), $now + 20);
check($s['effect']['flash'] && $s['effect']['colour'] === 'G', 'F flash uses area 1 colour');
loraq_apply_event($s, ev('TX1', 43, 0, [c('1')]), $now + 30);
check($s['effect'] === null, 'clear area 1 stops flash');

// Same seq from a different session or origin is a different event.
$s = loraq_empty_store();
loraq_apply_event($s, ev('TX1', 1, 0, [d('1', 'One')], 'AAAA'), $now);
check(loraq_apply_event($s, ev('TX1', 1, 0, [d('1', 'Two')], 'BBBB'), $now + 1) === 'applied', 'new session');
check(loraq_apply_event($s, ev('TX2', 1, 0, [d('1', 'Three')], 'BBBB'), $now + 2) === 'applied', 'new origin');
check($s['zones']['1']['text'] === 'Three', 'latest wins');

// Validation.
check(loraq_event_key(['origin' => 'TX 1', 'session' => 'AB12', 'seq' => 1]) === null, 'bad origin');
check(loraq_event_key(['origin' => 'TX1', 'session' => 'ab12', 'seq' => 1]) === null, 'bad session');
check(loraq_event_key(['origin' => 'TX1', 'session' => 'AB12', 'seq' => 0]) === null, 'bad seq');
check(loraq_event_key(['origin' => 'TX1', 'session' => 'AB12', 'seq' => '1']) === null, 'string seq');
check(loraq_event_key(['origin' => 'TX1', 'session' => 'AB12', 'seq' => 9999]) === 'TX1|AB12|9999', 'good key');
$base = ['origin' => 'TX1', 'session' => 'AB12', 'seq' => 1, 'age_ms' => 0];
check(is_string(loraq_validate_event($base + ['records' => []])), 'empty records');
check(is_string(loraq_validate_event($base + ['records' => [d('3', 'x')]])), 'bad area');
check(is_string(loraq_validate_event($base + ['records' => [d('1', 'x', 6000)]])), 'timer too big');
check(is_string(loraq_validate_event($base + ['records' => [d('1', 'x', null, 'P')]])), 'bad colour');
check(is_string(loraq_validate_event($base + ['records' => [d('1', 'x', null, 'W', 8)]])), 'bad effect');
check(is_string(loraq_validate_event($base + ['records' => [d('1', "\xff")]])), 'invalid utf8');
check(is_string(loraq_validate_event(['age_ms' => -1] + $base + ['records' => [d('1', 'x')]])), 'negative age');
check(is_array(loraq_validate_event($base + ['records' => [d('1', 'Café ✓', 5999)]])), 'unicode ok');

// Pruning and snapshot.
$s = loraq_empty_store();
loraq_apply_event($s, ev('TX1', 1, 0, [d('1', 'x')]), $now);
loraq_prune($s, $now + LORAQ_EVENT_TTL_MS + 1);
check($s['events'] === [], 'prune old events');
loraq_touch_reporter($s, 'tx-main', ['queue_depth' => 3, 'app_version' => '1.2'], $now);
$snap = loraq_snapshot($s);
check(!isset($snap['events']) && !isset($snap['zones']['1']['key']), 'snapshot hides internals');
check($snap['reporters'][0]['name'] === 'tx-main' && $snap['reporters'][0]['queue_depth'] === 3, 'snapshot reporters');
loraq_touch_reporter($s, 'tx-main', ['queue_depth' => 1, 'events' => [[]]], $now);
check(loraq_snapshot($s)['reporters'][0]['queue_depth'] === 0, 'queue depth excludes events in this push');
loraq_touch_reporter($s, 'tx-main', ['queue_depth' => 5, 'events' => [[], []]], $now);
check(loraq_snapshot($s)['reporters'][0]['queue_depth'] === 3, 'queue depth counts the backlog left');

echo ($failures ? "$failures of $count checks FAILED\n" : "All $count checks passed\n");
exit($failures ? 1 : 0);
