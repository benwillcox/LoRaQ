<?php
// Routing response tests. Run: php web/tests/route_test.php
declare(strict_types=1);

if (isset($argv[1])) {
    $_SERVER['SCRIPT_NAME'] = '/loraq/index.php';
    $_SERVER['REQUEST_URI'] = $argv[1];
    $_GET = json_decode(base64_decode($argv[2]), true);
    ob_start();
    register_shutdown_function(function (): void {
        $body = ob_get_clean();
        echo json_encode([http_response_code(), $body]);
    });
    require __DIR__ . '/../loraq/index.php';
    exit;
}

$missingKey = "No key specified. Open the private viewer link ending in /v/<view_key>.\n";
$cases = [
    ['/loraq/', [], $missingKey],
    ['/loraq/index.php', [], $missingKey],
    ['/loraq/v', [], $missingKey],
    ['/loraq/v/', [], $missingKey],
    ['/loraq/index.php', ['route' => 'v/'], $missingKey],
    ['/loraq/v/short', [], "Not found\n"],
    ['/loraq/unknown', [], "Not found\n"],
];

$failures = 0;
foreach ($cases as [$uri, $query, $expected]) {
    // The front controller exits, so each request needs its own PHP process.
    $output = [];
    $status = 0;
    exec(escapeshellarg(PHP_BINARY) . ' ' . escapeshellarg(__FILE__)
        . ' ' . escapeshellarg($uri)
        . ' ' . escapeshellarg(base64_encode(json_encode($query))), $output, $status);
    $response = json_decode(implode("\n", $output), true);
    if ($status !== 0 || $response !== [404, $expected]) {
        $failures++;
        echo "FAIL: $uri " . json_encode($query) . ': ' . json_encode($response) . "\n";
    }
}

echo count($cases) . " routing checks, $failures failures\n";
exit($failures === 0 ? 0 : 1);
