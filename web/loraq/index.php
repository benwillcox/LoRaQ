<?php
// LoRaQ web display front controller.
//   POST api/push                  pushers (transmitters / receivers) deliver cue events + heartbeats
//   GET  api/state?k=KEY&v=N       viewers poll the show snapshot
//   GET  v/KEY                     viewer page (the private URL)
//   GET  v/KEY/manifest.webmanifest
declare(strict_types=1);

require __DIR__ . '/app/bootstrap.php';

loraq_common_headers();
$route = loraq_route();
$method = $_SERVER['REQUEST_METHOD'] ?? 'GET';

if ($route === '' || $route === 'v') {
    not_found('No key specified. Open the private viewer link ending in /v/<view_key>.');
} elseif ($route === 'api/push') {
    if ($method !== 'POST') {
        header('Allow: POST');
        loraq_fail(405, 'Use POST');
    }
    handle_push();
} elseif ($route === 'api/state') {
    handle_state();
} elseif (preg_match('#^v/([A-Za-z0-9_-]{20,128})(/manifest\.webmanifest)?$#D', $route, $m)) {
    $show = loraq_find_view_key($m[1]);
    if ($show === null) {
        not_found();
    }
    empty($m[2]) ? render_viewer($show, $m[1]) : render_manifest($show, $m[1]);
} else {
    not_found();
}

function not_found(string $message = 'Not found'): void
{
    http_response_code(404);
    header('Content-Type: text/plain; charset=utf-8');
    echo $message . "\n";
    exit;
}

function handle_push(): void
{
    $length = (int) ($_SERVER['CONTENT_LENGTH'] ?? 0);
    if ($length > LORAQ_MAX_BODY) {
        loraq_fail(413, 'Body too large');
    }
    $show = loraq_find_token(loraq_bearer_token(), loraq_config()['shows']);
    if ($show === null) {
        usleep(200000);
        loraq_fail(401, 'Bad token');
    }
    $raw = file_get_contents('php://input', false, null, 0, LORAQ_MAX_BODY + 1);
    if ($raw === false || strlen($raw) > LORAQ_MAX_BODY) {
        loraq_fail(413, 'Body too large');
    }
    $body = json_decode($raw, true, 16);
    if (!is_array($body)) {
        loraq_fail(400, 'Body must be a JSON object');
    }
    $reporter = $body['reporter'] ?? null;
    if (!loraq_valid_station_id($reporter)) {
        loraq_fail(400, 'Push requires a reporter Station ID (1-16 letters, digits, underscores or hyphens)');
    }
    $events = $body['events'] ?? [];
    if (!is_array($events) || !loraq_is_list($events)) {
        loraq_fail(400, 'events must be a list');
    }
    if (count($events) > LORAQ_MAX_EVENTS_PER_PUSH) {
        loraq_fail(413, 'Too many events; send at most ' . LORAQ_MAX_EVENTS_PER_PUSH);
    }

    $valid = [];
    $rejected = [];
    foreach ($events as $i => $e) {
        $key = loraq_event_key($e);
        if ($key === null) {
            loraq_fail(400, "events[$i]: bad origin, session or seq");
        }
        $clean = loraq_validate_event($e);
        if (is_string($clean)) {
            $rejected[] = ['key' => [$e['origin'], $e['session'], $e['seq']], 'error' => $clean];
        } else {
            $valid[] = $clean;
        }
    }

    $acked = loraq_update_store($show, function (array &$store) use ($valid, $reporter, $body): array {
        $now = loraq_now_ms();
        // Oldest first, so a batch replayed after an outage settles on the newest state.
        usort($valid, function ($a, $b) {
            return $b['age_ms'] <=> $a['age_ms'];
        });
        $acked = [];
        foreach ($valid as $ev) {
            loraq_apply_event($store, $ev, $now);
            $acked[] = [$ev['origin'], $ev['session'], $ev['seq']];
        }
        loraq_touch_reporter($store, $reporter, $body, $now);
        return $acked;
    });
    loraq_json(200, ['acked' => $acked, 'rejected' => $rejected, 'server_time' => loraq_now_ms()]);
}

function handle_state(): void
{
    $key = $_GET['k'] ?? '';
    $show = is_string($key) ? loraq_find_view_key($key) : null;
    if ($show === null) {
        loraq_fail(404, 'Unknown show');
    }
    $snap = loraq_read_snapshot($show);
    $now = loraq_now_ms();
    $known = $_GET['v'] ?? null;
    if (is_string($known) && ctype_digit($known) && (int) $known === $snap['v']) {
        loraq_json(200, ['v' => $snap['v'], 'same' => true, 'now' => $now]);
    }
    $snap['now'] = $now;
    $snap['title'] = loraq_config()['shows'][$show]['title'];
    loraq_json(200, $snap);
}

function render_viewer(string $show, string $key): void
{
    $base = loraq_base_path();
    $showConfig = loraq_config()['shows'][$show];
    $title = loraq_html($showConfig['title']);
    $appName = loraq_html($showConfig['app_name']);
    $appleIcon = loraq_show_icon_asset($showConfig, 180);
    $asset = function (string $name) use ($base): string {
        $file = __DIR__ . "/static/$name";
        return loraq_html($base . "static/$name?" . (is_file($file) ? filemtime($file) : 0));
    };
    header('Content-Type: text/html; charset=utf-8');
    header("Content-Security-Policy: default-src 'self'; img-src 'self' data:; base-uri 'none'; "
         . "form-action 'none'; frame-ancestors 'none'; object-src 'none'");
    $api = loraq_html($base . 'api/state');
    $manifest = loraq_html($base . "v/$key/manifest.webmanifest");
    $keyAttr = loraq_html($key);
    echo <<<HTML
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="robots" content="noindex, nofollow">
<meta name="theme-color" content="#091b58">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="{$appName}">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<title>{$title}</title>
<link rel="manifest" href="{$manifest}">
<link rel="icon" href="{$asset('icon.svg')}" type="image/svg+xml">
<link rel="apple-touch-icon" sizes="180x180" href="{$asset($appleIcon)}">
<link rel="stylesheet" href="{$asset('viewer.css')}">
<script src="{$asset('viewer.js')}" defer></script>
</head>
<body data-key="{$keyAttr}" data-api="{$api}">
<div id="screen" class="screen">
  <div id="bar" class="bar">
    <div class="bar-left"><button id="status" class="pill pill-wait" type="button" aria-haspopup="true" aria-expanded="false" aria-controls="stations">CONNECTING</button></div>
    <div id="show-title" class="show-title" title="{$title}">{$title}</div>
    <div class="bar-right">
      <button id="share" class="btn" type="button" aria-label="Share display link" title="Share display link" hidden>
        <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
          <circle cx="18" cy="5" r="3"/>
          <circle cx="6" cy="12" r="3"/>
          <circle cx="18" cy="19" r="3"/>
          <path d="m8.6 10.5 6.8-4m-6.8 7.5 6.8 4"/>
        </svg>
      </button>
      <button id="sound" class="btn" type="button" aria-label="Sound" aria-pressed="false" title="Sound off">
        <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
          <path d="M11 4 6 8H3v8h3l5 4Z"/>
          <path class="icon-off" d="m16 9 6 6m0-6-6 6"/>
          <path class="icon-on" d="M15 8a6 6 0 0 1 0 8M18 5a10 10 0 0 1 0 14"/>
        </svg>
      </button>
    </div>
  </div>
  <div id="stations" class="stations" role="dialog" aria-label="Sending stations" hidden>
    <p id="stations-detail" class="stations-detail"></p>
    <ul id="stations-list" class="stations-list"></ul>
    <p id="stations-last" class="stations-last" hidden></p>
  </div>
  <div id="area1" class="area1">
    <div id="a1-text" class="a1-text"></div>
    <div id="a1-timer" class="timer a1-timer" hidden></div>
  </div>
  <div id="area2" class="area2">
    <div class="a2-label">CURRENT CUE</div>
    <div class="a2-row">
      <div id="a2-text" class="a2-text"></div>
      <div id="a2-timer" class="timer a2-timer" hidden></div>
    </div>
  </div>
  <noscript><div class="noscript">This display needs JavaScript.</div></noscript>
</div>
</body>
</html>
HTML;
    exit;
}

function render_manifest(string $show, string $key): void
{
    $base = loraq_base_path();
    $showConfig = loraq_config()['shows'][$show];
    $icon = function (int $size) use ($base, $showConfig): array {
        $name = loraq_show_icon_asset($showConfig, $size);
        $file = __DIR__ . "/static/$name";
        return [
            'src' => $base . "static/$name?" . filemtime($file),
            'sizes' => $size . 'x' . $size,
            'type' => 'image/png',
        ];
    };
    header('Content-Type: application/manifest+json; charset=utf-8');
    echo json_encode([
        'name' => $showConfig['app_name'],
        'short_name' => $showConfig['app_name'],
        'start_url' => $base . "v/$key",
        'scope' => $base . "v/$key",
        'display' => 'standalone',
        'background_color' => '#091b58',
        'theme_color' => '#091b58',
        'icons' => [$icon(192), $icon(512)],
    ], JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE);
    exit;
}
