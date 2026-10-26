#!/bin/sh
set -eu

if [ "$(uname -s)" != "Linux" ]; then
    echo "Build the Linux/Pi executables on Linux." >&2
    exit 1
fi

if ! command -v objdump >/dev/null 2>&1; then
    echo "Missing objdump. On Raspberry Pi OS/DietPi, run: sudo apt install binutils" >&2
    exit 1
fi

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$root"
python=${PYTHON:-python3}
"$python" -c 'import sys, pygame, serial.tools.list_ports, pythonosc; print("Building with:", sys.executable)' || {
    echo "Select a Python environment with pygame, pyserial and python-osc installed. Set PYTHON to its interpreter if needed." >&2
    exit 1
}
apps="LoRaQ-transmitter LoRaQ-display LoRaQ-adapter-configure"
for app in $apps; do
    if [ -d "$root/dist/linux/$app" ]; then
        echo "Old folder-based build found at dist/linux/$app. Move it aside before building; preserve its loraq.ini settings." >&2
        exit 1
    fi
done
if ! "$python" -c 'import importlib.util, sys; sys.exit(importlib.util.find_spec("PyInstaller") is None)'; then
    build_env="$root/build/linux-env"
    echo "PyInstaller is missing; using build-only environment: $build_env"
    if [ ! -e "$build_env" ]; then
        "$python" -m venv --system-site-packages "$build_env" || {
            echo "Could not create the build environment. On Raspberry Pi OS/DietPi, install python3-venv with apt, then retry." >&2
            exit 1
        }
    fi
    python="$build_env/bin/python"
    if [ ! -x "$python" ] || [ ! -f "$build_env/pyvenv.cfg" ]; then
        echo "Invalid build environment at $build_env. Move it aside and retry." >&2
        exit 1
    fi
    if ! "$python" -c 'import sys; sys.exit(sys.prefix == sys.base_prefix)'; then
        echo "Invalid build environment at $build_env. Move it aside and retry." >&2
        exit 1
    fi
    "$python" -c 'import pygame, serial.tools.list_ports, pythonosc' || {
        echo "The build-only environment cannot access the runtime dependencies. If they live in an isolated environment, install PyInstaller there and select it with PYTHON." >&2
        exit 1
    }
    if ! "$python" -c 'import importlib.util, sys; sys.exit(importlib.util.find_spec("PyInstaller") is None)'; then
        "$python" -m pip install pyinstaller || {
            echo "PyInstaller installation failed in $build_env. Check the pip error above and retry." >&2
            exit 1
        }
    fi
fi
"$python" -c 'import sys, PyInstaller; print("Packaging with:", sys.executable)'
for app in $apps; do
    "$python" -m PyInstaller --noconfirm --clean --onefile --console \
        --add-data "$root/assets:assets" \
        --name "$app" --distpath "$root/dist/linux" \
        --workpath "$root/build/linux" --specpath "$root/build" "$root/$app.py"
done
config="$root/dist/linux/loraq.ini"
if [ ! -e "$config" ]; then
    cp "$root/loraq.ini" "$config"
fi
for app in $apps; do
    printf 'Built %s\n' "$root/dist/linux/$app"
done
printf 'Configuration: %s\n' "$config"
