#!/bin/sh

set -eu
launcher_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

if [ "${1-}" = "--terminal" ]; then
    # Use the real Terminal stdin for interactive commands, not a pipe from the launcher.
    shift
    set +e
    "$launcher_dir/../Resources/application/@APP_NAME@" "$@"
    result=$?
    printf '\nLoRaQ exited (status %s). Press Return to close this session.\n' "$result"
    read -r reply
    exit "$result"
fi

if /usr/bin/open -a Terminal "$launcher_dir/LoRaQ-terminal.command"
then
    exit 0
fi

/usr/bin/osascript -e 'display alert "LoRaQ could not open Terminal" message "Check that Terminal is available and try opening LoRaQ again." as critical'
exit 1
