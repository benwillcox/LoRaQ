#!/bin/sh

set -eu
launcher_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec /bin/sh "$launcher_dir/LoRaQ-launcher" --terminal
