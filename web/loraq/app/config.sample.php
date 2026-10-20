<?php
// Copy this file to config.php and edit it. config.php must never be web-readable (app/.htaccess denies it).
//
// Generate each key/token with:   php -r "echo bin2hex(random_bytes(24)), PHP_EOL;"
//   or in Python:                 python -c "import secrets; print(secrets.token_urlsafe(32))"
//
// view_key    : the secret part of the viewer URL  https://example.com/loraq/v/<view_key>
//               (20-128 chars of A-Z a-z 0-9 _ -). Change it to revoke all shared links.
// push_tokens : one token per pushing device (the name is shown on the page and in diagnostics).
//               Put the same token in that device's loraq.ini [web_push] token = ...
//               (16-128 chars of A-Z a-z 0-9 _ -). Remove a line to revoke that device.
return [
    // Where state files are stored. Must be writable by PHP. Outside the web root is best,
    // e.g. '/home/USER/loraq-data'. The default app/data is protected by app/.htaccess.
    'data_dir' => __DIR__ . '/data',

    'shows' => [
        // The array key is the show id (A-Z a-z 0-9 _ -). Add more entries to host several shows.
        'main' => [
            'title' => 'LoRaQ Show',
            'view_key' => 'CHANGE-ME-to-a-long-random-string',
            'push_tokens' => [
                'tx-main' => 'CHANGE-ME-token-for-main-transmitter',
                'rx-booth' => 'CHANGE-ME-token-for-booth-receiver',
            ],
        ],
    ],
];
