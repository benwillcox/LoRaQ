"""Build Finder-launchable LoRaQ applications with Terminal sessions."""
from __future__ import annotations

import importlib.util
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

APP_NAMES = ("LoRaQ-transmitter", "LoRaQ-display", "LoRaQ-adapter-configure")


def create_iconset(directory: Path) -> None:
    import pygame
    from loraq_icon import create_app_icon

    directory.mkdir()
    pygame.font.init()
    try:
        for size in (16, 32, 128, 256, 512):
            # Choose artwork by logical size, including high-DPI representations.
            image = create_app_icon(size)
            for scale in (1, 2):
                suffix = "@2x" if scale == 2 else ""
                scaled = pygame.transform.smoothscale(image, (size * scale, size * scale))
                pygame.image.save(scaled, str(directory / f"icon_{size}x{size}{suffix}.png"))
    finally:
        pygame.font.quit()


def assemble_app(app: Path, payload: Path, icon: Path, app_name: str) -> None:
    contents = app / "Contents"
    macos = contents / "MacOS"
    resources = contents / "Resources"
    macos.mkdir(parents=True)
    resources.mkdir()
    shutil.copytree(payload, resources / "application", symlinks=True)
    shutil.copy2(icon, resources / "LoRaQ.icns")
    launcher = macos / "LoRaQ-launcher"
    with launcher.open("w", encoding="utf-8", newline="\n") as output:
        output.write(Path(__file__).with_name("launcher.sh").read_text(encoding="utf-8")
                     .replace("@APP_NAME@", app_name))
    launcher.chmod(0o755)
    terminal_command = macos / "LoRaQ-terminal.command"
    with terminal_command.open("w", encoding="utf-8", newline="\n") as output:
        output.write(Path(__file__).with_name("terminal.command").read_text(encoding="utf-8"))
    terminal_command.chmod(0o755)
    with (contents / "Info.plist").open("wb") as output:
        plistlib.dump({
            "CFBundleExecutable": launcher.name,
            "CFBundleIconFile": "LoRaQ.icns",
            "CFBundleIdentifier": "org.loraq." + app_name[len("LoRaQ-"):],
            "CFBundleName": app_name,
            "CFBundlePackageType": "APPL",
            "CFBundleVersion": "1",
            "LSUIElement": True,
            "NSHighResolutionCapable": True,
        }, output)


def main() -> None:
    if sys.platform != "darwin":
        raise SystemExit("Build this application on macOS; PyInstaller cannot cross-compile it.")
    missing = [name for name in ("PyInstaller", "serial", "pythonosc", "pygame")
               if importlib.util.find_spec(name) is None]
    if missing:
        raise SystemExit(
            f"Missing build dependencies: {', '.join(missing)}. "
            "Run packaging/build-macos.sh to prepare the build environment."
        )
    destination = ROOT / "dist" / "LoRaQ"
    for app_name in APP_NAMES:
        app = destination / f"{app_name}.app"
        if app.exists():
            raise SystemExit(
                f"{app} already exists. Move that app out of the output folder before rebuilding; "
                "keep loraq.ini to preserve your settings."
            )
    with tempfile.TemporaryDirectory(prefix="loraq-macos-") as temporary:
        work = Path(temporary)
        iconset = work / "LoRaQ.iconset"
        create_iconset(iconset)
        icon = work / "LoRaQ.icns"
        subprocess.run(["/usr/bin/iconutil", "-c", "icns", str(iconset), "-o", str(icon)],
                       check=True)
        for app_name in APP_NAMES:
            subprocess.run([
                sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
                "--onedir", "--console", "--name", app_name,
                "--add-data", str(ROOT / "assets") + os.pathsep + "assets",
                "--distpath", str(work / "dist"), "--workpath", str(work / "build"),
                "--specpath", str(work), str(ROOT / f"{app_name}.py"),
            ], cwd=ROOT, check=True)
            assemble_app(work / f"{app_name}.app", work / "dist" / app_name, icon, app_name)
        destination.mkdir(parents=True, exist_ok=True)
        for app_name in APP_NAMES:
            shutil.copytree(work / f"{app_name}.app", destination / f"{app_name}.app", symlinks=True)
        config = destination / "loraq.ini"
        if not config.exists():
            shutil.copy2(ROOT / "loraq.ini", config)
    for app_name in APP_NAMES:
        print(f"Built {destination / (app_name + '.app')}")
    print(f"Edit {config}, then double-click an app.")


if __name__ == "__main__":
    main()
