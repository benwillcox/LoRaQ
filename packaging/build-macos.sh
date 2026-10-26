#!/bin/sh
set -eu

if [ "$(uname -s)" != "Darwin" ]; then
    echo "Build the macOS application on a Mac." >&2
    exit 1
fi

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$root"
python=${PYTHON:-python3}
build_env="$root/build/macos-env"
if [ ! -e "$build_env" ]; then
    "$python" -m venv "$build_env" || {
        echo "Could not create the build environment. Install Python with venv support or select it with PYTHON." >&2
        exit 1
    }
fi
python="$build_env/bin/python"
if [ ! -x "$python" ]; then
    echo "Invalid build environment at $build_env. Move it aside and retry." >&2
    exit 1
fi
"$python" -m pip install --upgrade pip || {
    echo "Could not update pip in the build environment. Check the pip error above and retry." >&2
    exit 1
}
"$python" -m pip install --only-binary=pygame-ce -r "$root/packaging/requirements.txt" || {
    echo "Build dependency installation failed. Check the pip error above." >&2
    exit 1
}
"$python" -c 'import sys, pygame, serial.tools.list_ports, pythonosc, PyInstaller; print("Building with:", sys.executable)' || {
    echo "Runtime imports failed in the build environment." >&2
    exit 1
}
exec "$python" "$root/packaging/macos/build.py"
