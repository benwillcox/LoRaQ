<?php
// Shared helpers: configuration, locked JSON file storage and HTTP utilities.
declare(strict_types=1);

require __DIR__ . '/merge.php';

const LORAQ_MAX_BODY = 262144;
const LORAQ_MAX_EVENTS_PER_PUSH = 100;

function loraq_config(): array
{
    static $config = null;
    if ($config === null) {
        $file = __DIR__ . '/config.php';
        if (!is_file($file)) {
            loraq_fail(500, 'Server not configured: copy app/config.sample.php to app/config.php');
        }
        $config = require $file;
        $config['data_dir'] = rtrim($config['data_dir'] ?? (__DIR__ . '/data'), '/\\');
        $shows = [];
        foreach ($config['shows'] ?? [] as $id => $show) {
            // Unsafe or placeholder shows are ignored rather than served.
            $id = (string) $id;
            $key = (string) ($show['view_key'] ?? '');
            if (!preg_match('/^[A-Za-z0-9_-]{1,40}$/D', $id) || !preg_match('/^[A-Za-z0-9_-]{20,128}$/D', $key)
                    || stripos($key, 'CHANGE') !== false) {
                error_log("LoRaQ: show '$id' ignored (needs an id and a random view_key of 20+ characters)");
                continue;
            }
            $tokens = [];
            foreach ($show['push_tokens'] ?? [] as $reporter => $token) {
                $token = (string) $token;
                if (preg_match('/^[A-Za-z0-9_-]{16,128}$/D', $token) && stripos($token, 'CHANGE') === false) {
                    $tokens[(string) $reporter] = $token;
                } else {
                    error_log("LoRaQ: push token for '$reporter' in show '$id' ignored (needs 16+ random characters)");
                }
            }
            $shows[$id] = ['title' => (string) ($show['title'] ?? 'LoRaQ'), 'view_key' => $key, 'push_tokens' => $tokens];
        }
        $config['shows'] = $shows;
    }
    return $config;
}

function loraq_find_view_key(string $key): ?string
{
    $found = null;
    foreach (loraq_config()['shows'] as $id => $show) {
        if (hash_equals($show['view_key'], $key)) {
            $found = $id;
        }
    }
    return $found;
}

/** Returns [show_id, reporter] for a push token. Checks every token to keep timing uniform. */
function loraq_find_token(string $token): ?array
{
    $found = null;
    foreach (loraq_config()['shows'] as $id => $show) {
        foreach ($show['push_tokens'] as $reporter => $candidate) {
            if (hash_equals($candidate, $token)) {
                $found = [$id, $reporter];
            }
        }
    }
    return $found;
}

function loraq_paths(string $show): array
{
    $dir = loraq_config()['data_dir'];
    if (!is_dir($dir) && !@mkdir($dir, 0770, true) && !is_dir($dir)) {
        loraq_fail(500, 'Data directory is not writable');
    }
    return ["$dir/$show.store.json", "$dir/$show.state.json", "$dir/$show.lock"];
}

function loraq_write_atomic(string $file, string $data): void
{
    $tmp = $file . '.' . bin2hex(random_bytes(4)) . '.tmp';
    if (file_put_contents($tmp, $data) === false || !rename($tmp, $file)) {
        @unlink($tmp);
        loraq_fail(500, 'Could not save state');
    }
}

/** Runs $fn(&$store) under an exclusive lock, then saves the store and publishes the viewer snapshot. */
function loraq_update_store(string $show, callable $fn)
{
    [$storeFile, $stateFile, $lockFile] = loraq_paths($show);
    $lock = fopen($lockFile, 'c');
    if ($lock === false || !flock($lock, LOCK_EX)) {
        loraq_fail(500, 'Could not lock state');
    }
    try {
        $raw = is_file($storeFile) ? file_get_contents($storeFile) : false;
        $store = $raw ? json_decode($raw, true) : null;
        if (!is_array($store)) {
            $store = loraq_empty_store();
        }
        $result = $fn($store);
        $now = loraq_now_ms();
        $store['updated_ms'] = $now;
        loraq_prune($store, $now);
        $flags = JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES | JSON_INVALID_UTF8_SUBSTITUTE;
        loraq_write_atomic($storeFile, json_encode($store, $flags));
        loraq_write_atomic($stateFile, json_encode(loraq_snapshot($store), $flags));
        return $result;
    } finally {
        flock($lock, LOCK_UN);
        fclose($lock);
    }
}

function loraq_read_snapshot(string $show): array
{
    [, $stateFile] = loraq_paths($show);
    $raw = is_file($stateFile) ? @file_get_contents($stateFile) : false;
    $snap = $raw ? json_decode($raw, true) : null;
    return is_array($snap) ? $snap : loraq_snapshot(loraq_empty_store());
}

function loraq_base_path(): string
{
    $dir = str_replace('\\', '/', dirname($_SERVER['SCRIPT_NAME'] ?? '/index.php'));
    return rtrim($dir, '/') . '/';
}

function loraq_route(): string
{
    if (isset($_GET['route']) && is_string($_GET['route'])) {
        return trim($_GET['route'], '/');
    }
    $path = parse_url($_SERVER['REQUEST_URI'] ?? '/', PHP_URL_PATH) ?: '/';
    $base = loraq_base_path();
    if (strncmp($path, $base, strlen($base)) === 0) {
        $path = substr($path, strlen($base));
    }
    $path = preg_replace('#^index\.php(/|$)#', '', ltrim($path, '/'));
    return trim(rawurldecode($path), '/');
}

function loraq_common_headers(): void
{
    header('X-Robots-Tag: noindex, nofollow, noarchive');
    header('Referrer-Policy: no-referrer');
    header('X-Content-Type-Options: nosniff');
    header('Cache-Control: no-store, max-age=0');
    if (!empty($_SERVER['HTTPS']) && $_SERVER['HTTPS'] !== 'off') {
        header('Strict-Transport-Security: max-age=31536000');
    }
}

function loraq_json(int $status, array $body): void
{
    http_response_code($status);
    header('Content-Type: application/json; charset=utf-8');
    echo json_encode($body, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES | JSON_INVALID_UTF8_SUBSTITUTE);
    exit;
}

function loraq_fail(int $status, string $message): void
{
    loraq_json($status, ['error' => $message]);
}

function loraq_bearer_token(): string
{
    foreach (['HTTP_AUTHORIZATION', 'REDIRECT_HTTP_AUTHORIZATION'] as $name) {
        $value = $_SERVER[$name] ?? '';
        if (is_string($value) && preg_match('/^Bearer\s+(\S+)$/i', trim($value), $m)) {
            return $m[1];
        }
    }
    if (function_exists('getallheaders')) {
        foreach (getallheaders() as $name => $value) {
            if (strcasecmp($name, 'Authorization') === 0 && preg_match('/^Bearer\s+(\S+)$/i', trim($value), $m)) {
                return $m[1];
            }
        }
    }
    // Fallback for hosts that strip Authorization before PHP sees it.
    return is_string($_SERVER['HTTP_X_LORAQ_TOKEN'] ?? null) ? trim($_SERVER['HTTP_X_LORAQ_TOKEN']) : '';
}

function loraq_html(string $text): string
{
    return htmlspecialchars($text, ENT_QUOTES | ENT_SUBSTITUTE, 'UTF-8');
}
