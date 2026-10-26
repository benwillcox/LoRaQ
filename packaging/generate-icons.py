"""Regenerate sized PNGs, Windows ICO and web icons from the two raster masters."""
import argparse
import io
import shutil
import struct
import sys
from pathlib import Path

import pygame

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from loraq_icon import create_app_icon


def create_ico(path: Path) -> None:
    sizes = (16, 24, 32, 48, 64, 128, 256)
    frames = []
    for size in sizes:
        stream = io.BytesIO()
        pygame.image.save(create_app_icon(size), stream, "icon.png")
        frames.append(stream.getvalue())
    offset = 6 + 16 * len(frames)
    entries = []
    for size, frame in zip(sizes, frames):
        entries.append(struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0,
                                   1, 32, len(frame), offset))
        offset += len(frame)
    path.write_bytes(struct.pack("<HHH", 0, 1, len(frames))
                     + b"".join(entries) + b"".join(frames))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    icons = ROOT / "packaging" / "icons"
    for size in (16, 24, 32, 48, 64, 96, 128, 180, 192, 256, 512, 1024):
        pygame.image.save(create_app_icon(size), str(icons / f"icon-{size}.png"))
    ico = ROOT / "packaging" / "windows" / "loraq.ico"
    create_ico(ico)
    viewer = ROOT / "web" / "loraq" / "static"
    shutil.copy2(icons / "lq.svg", viewer / "icon.svg")
    for size in (180, 192, 512):
        shutil.copy2(icons / f"icon-{size}.png", viewer / f"icon-{size}.png")
    print("Generated adaptive icons (LoRaQ >=64 px; LQ <64 px).")


if __name__ == "__main__":
    main()
