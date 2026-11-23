# Build commands

Run from the project root on the target platform. Python must be installed.
Scripts prepare/reuse build environments automatically and preserve existing output INI files.

Each platform build produces all three applications:

- `LoRaQ-transmitter` - transmitter console and graphical preview.
- `LoRaQ-display` - receiver display (fullscreen by default; `--windowed` is available).
- `LoRaQ-adapter-configure` - console-based Pi HAT / EByte USB adapter configuration tool.

All three use the same external `loraq.ini` in their output directory. Stop any
running application or service using the adapter before configuring it, and do
not run a transmitter and display against the same serial adapter simultaneously.
The configure tool uses the same `[link]` auto-detection as the other apps and
requires the adapter to be in configuration mode; see the
[adapter configuration guide](../docs/lora_adapter_config.md).

## Windows

```powershell
.\packaging\build-windows.ps1
```

Output in `dist/windows/`:

- `LoRaQ-transmitter.exe`
- `LoRaQ-display.exe`
- `LoRaQ-adapter-configure.exe`
- `loraq.ini` (preserved if already present)

Optional initial Python selection:

```powershell
.\packaging\build-windows.ps1 -Python "C:\path\to\python.exe"
```

Double-click the configure executable to open its console. After configuration
succeeds or fails, press Return to close the window. When redirecting input for
automation, the executable exits without this prompt.

## macOS

```bash
sh packaging/build-macos.sh
```

Output in `dist/LoRaQ/`:

- `LoRaQ-transmitter.app`
- `LoRaQ-display.app`
- `LoRaQ-adapter-configure.app`
- `loraq.ini` (preserved if already present)

Move any previous output `.app` bundles aside before rebuilding. Apps are not signed/notarized.
The script updates pip inside the build environment before installing dependencies.

For a persistent Dock icon, drag the `.app` from Finder into the Dock rather than
keeping the running preview process. Keep the app at that location with
`loraq.ini` beside it. The launcher opens a bundled Terminal command file so
interactive commands and live logs are available without pressing Return to start.
Each app has its own Dock identity. Terminal also shows the configure tool's
results and display logs; after an application exits, press Return to finish its
Terminal session. For command-line options, run the executable inside the bundle,
for example:

```bash
./dist/LoRaQ/LoRaQ-display.app/Contents/Resources/application/LoRaQ-display --windowed
```

Optional initial Python selection:

```bash
PYTHON=python3.13 sh packaging/build-macos.sh
```

## Linux / Raspberry Pi

Install pygame and pyserial with apt, then install python-osc and PyInstaller
in a virtual environment. Run these commands from the project root:

```bash
sudo apt install binutils python3-venv python3-pygame python3-serial
sudo apt install "libpython$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
python3 -m venv --system-site-packages build/linux-packaging-env
build/linux-packaging-env/bin/python -m pip install python-osc pyinstaller
PYTHON="$PWD/build/linux-packaging-env/bin/python" sh packaging/build-linux.sh
```

The `libpython` package must match the Python version used to build.
The virtual environment can access the apt-installed runtime dependencies;
`python-osc` is installed there because it may not be available from apt.

When using another Python environment that already has the runtime dependencies
but lacks PyInstaller, the script creates and reuses `build/linux-env`, a virtual
environment that can access system-installed runtime dependencies. It installs
PyInstaller there, not into the OS-managed Python installation.

If that environment is invalid, or pip reports `externally-managed-environment`
while using it, move it aside from the project directory and retry:

```bash
mv build/linux-env "build/linux-env.old-$(date +%Y%m%d-%H%M%S)"
sh packaging/build-linux.sh
```

This preserves the old environment and lets the script create a fresh one.
Do not use `sudo pip` or `--break-system-packages` for this build.

Output in `dist/linux/`:

- `LoRaQ-transmitter`
- `LoRaQ-display`
- `LoRaQ-adapter-configure`
- `loraq.ini` (preserved if already present)

Move any old folder-based build at one of these executable paths aside first.
Launch Linux console applications from a terminal to see their output.

Optional Python selection:

```bash
PYTHON="$HOME/loraq-build-env/bin/python" sh packaging/build-linux.sh
```

Test with the existing service stopped:

```bash
sudo systemctl stop loraq-display.service
./dist/linux/LoRaQ-display
```

## Distribution

- Keep the app/executable and `loraq.ini` together.
- Source installations need the root-level `assets/` folder beside the Python scripts,
  not `packaging/`. Built applications bundle these runtime assets automatically.
- GUI windows use the branded icon. Terminal/console windows retain their host application's icon.
- Icons and startup screens share Area 1's navy `#091b58`, with amber Q `#ffd166`:
  LoRaQ at 64px+, LQ below 64px.
- Edit the INI for each station; restart after changes. `LORAQ_CONFIG` overrides its location.
- Remove private keys/tokens before sharing configuration.
- Linux builds must match the target architecture and compatible OS libraries.
- To change Mac/Windows build Python, move `build/macos-env` or `build/windows-env` aside and rerun.

## Regenerating assets

The SVG masters and generated icon sizes live in `icons`. The two 1024px PNG
runtime masters live in `../assets/`. The PNGs freeze the Arial lettering across
platforms; re-render them in a browser if editing the SVG artwork, then update
the matching PNG in `../assets/`.

```powershell
python packaging/generate-icons.py
python packaging/render-screenshots.py
```

Both commands require pygame and update assets in this repository only.
The icon command writes PNG sizes, Windows ICO and web-viewer icons. Mac ICNS
is generated during the Mac build; its artwork uses logical size for Retina.
Runtime SDL windows use LQ on Windows/Linux and LoRaQ on macOS; SDL supplies
one image, so those runtime icons cannot switch artwork as the OS scales them.
Screenshots use example cues and simulated telemetry. Add `--mobile-source "capture.png"`
to the screenshot command to frame a portrait capture of the web viewer.
