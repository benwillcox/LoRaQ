"""Render repeatable demo screenshots with the current graphics engine."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

import pygame

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from loraq_gfx_engine import COLOUR_MAP, LoRaQEngine
from loraq_protocol import Record


def cue_display(telemetry: bool) -> tuple[pygame.Surface, LoRaQEngine]:
    surface = pygame.Surface((800, 633 if telemetry else 450))
    engine = LoRaQEngine(surface, show_telemetry=telemetry, show_tx_info=False)
    records = ([Record("D", "1", "G", 15, text="GO Sound 7"),
                Record("D", "2", "W", 45, text="Scene change - stage left")]
               if telemetry else
               [Record("D", "1", "W", 30, text="Stand by LX 12"),
                Record("D", "2", "Y", 10, text="House to half")])
    with patch("loraq_gfx_engine.time.monotonic", return_value=100.0):
        engine.apply_records(records)
        engine.set_sent_seq(2)
        engine.set_stations([
            ("10_incher", 1, "OK", "ACK #2 [RSSI: -30.0 dBm]"),
            ("Steam", 0, "NO_RESPONSE", "NO ACK x2 / TX #2"),
        ])
        engine.set_remote_cue("REMOTE", "ABCD", 5)
        engine.remote_cue = "REMOTE CUE REMOTE #5 @ 17:01:14"
        engine.update_duty_cycle(0.01, 0.4, 77.1)
        engine.render()
    # Finish the slide while retaining the demo countdown values.
    engine.state["area1_start_time"] = 102.0
    engine.state["area2_start_time"] = 102.0
    with patch("loraq_gfx_engine.time.monotonic", return_value=102.0):
        engine.render()
    return surface, engine


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mobile-source", type=Path,
                        help="Portrait browser screenshot to place in the phone frame")
    args = parser.parse_args()
    images = ROOT / "images"
    pygame.font.init()
    try:
        receiver, _ = cue_display(False)
        transmitter, engine = cue_display(True)
        pygame.image.save(receiver, str(images / "receiver.png"))
        pygame.image.save(transmitter, str(images / "transmitter.png"))
        pygame.image.save(transmitter, str(images / "palette-preview.png"))
        pygame.image.save(transmitter.subsurface(engine.stats_rect),
                          str(images / "transmitter-stats.png"))
        palette = pygame.Surface((1200, 450))
        for index, colour in enumerate(COLOUR_MAP):
            tile = pygame.Surface((400, 225))
            example = LoRaQEngine(tile, show_tx_info=False)
            example.apply_records([Record("D", "1", colour, text=f"{colour} / Stand by")])
            example.render()
            palette.blit(tile, ((index % 3) * 400, (index // 3) * 225))
        pygame.image.save(palette, str(images / "cue-colours.png"))
        if args.mobile_source is not None:
            source = pygame.image.load(str(args.mobile_source))
            if source.get_height() <= source.get_width():
                parser.error("--mobile-source must be a portrait screenshot")
            phone = pygame.Surface((210, 454), pygame.SRCALPHA)
            pygame.draw.rect(phone, "#141820", phone.get_rect(), border_radius=24)
            pygame.draw.rect(phone, "#252c3d", phone.get_rect(), width=2, border_radius=24)
            screen = pygame.Surface((198, 428), pygame.SRCALPHA)
            screen.blit(pygame.transform.smoothscale(source, screen.get_size()), (0, 0))
            mask = pygame.Surface(screen.get_size(), pygame.SRCALPHA)
            pygame.draw.rect(mask, "white", mask.get_rect(), border_radius=16)
            screen.blit(mask, (0, 0), special_flags=pygame.BLEND_RGBA_MULT)
            phone.blit(screen, (6, 13))
            pygame.image.save(phone, str(images / "mobile-web-push-screenshot.png"))
    finally:
        pygame.font.quit()
    print("Rendered receiver, transmitter, stats, palette and cue-colour screenshots.")


if __name__ == "__main__":
    main()
