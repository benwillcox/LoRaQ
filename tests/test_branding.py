import importlib.util
import re
import unittest
from pathlib import Path
from unittest.mock import patch

import pygame

from loraq_gfx_engine import AREA1_BG, COLOUR_MAP, STATION_STATE_COLOURS, VERSION, LoRaQEngine
from loraq_icon import STARTUP_BG, create_app_icon, load_startup_logo
from loraq_protocol import Record


ROOT = Path(__file__).resolve().parents[1]


class StartupScreenTests(unittest.TestCase):
    def setUp(self):
        pygame.font.init()
        self.addCleanup(pygame.font.quit)

    def test_logo_reuses_icon_lettering_without_square_padding(self):
        logo = load_startup_logo()
        self.assertGreater(logo.get_width(), logo.get_height() * 3)
        self.assertEqual(logo.get_at((0, 0)).a, 0)
        colours = {tuple(logo.get_at((x, y)))
                   for x in range(0, logo.get_width(), 4)
                   for y in range(0, logo.get_height(), 4)}
        self.assertIn((255, 255, 255, 255), colours)
        self.assertIn((255, 209, 102, 255), colours)

    def test_receiver_and_preview_share_startup_artwork(self):
        receiver = LoRaQEngine(pygame.Surface((800, 450)), station_id="CAB_1", ack_slot=3)
        preview = LoRaQEngine(pygame.Surface((800, 633)), show_telemetry=True,
                             station_id="CAB_1", ack_slot=3)
        for engine in (receiver, preview):
            engine.render()
            self.assertEqual(engine.screen.get_at((0, 0))[:3], STARTUP_BG)
            self.assertEqual(engine.state["area1_text"],
                             f"Station ID: CAB_1 [3]\nVersion {VERSION}\n"
                             "Initialising serial device...")
            self.assertGreater(engine._startup_surfaces[0][0].get_width(), engine.width * 0.85)
            subtitle = engine._startup_surfaces[1][0]
            status_font = pygame.font.Font(None, int(engine.height * 0.055))
            self.assertGreaterEqual(subtitle.get_height(), status_font.get_height() * 2.8)
            logo, logo_rect = engine._startup_surfaces[0]
            subtitle_rect = engine._startup_surfaces[1][1]
            self.assertLessEqual(subtitle_rect.top - logo_rect.top - logo.get_bounding_rect().bottom,
                                 engine.height * 0.01)
            self.assertGreaterEqual(engine._startup_surfaces[2][1].top - subtitle_rect.bottom,
                                    engine.height * 0.10)
            for (surface, _), line in zip(engine._startup_surfaces[2:],
                                          engine.state["area1_text"].splitlines()):
                self.assertEqual(surface.get_size(), status_font.render(line, True, "white").get_size())
        self.assertEqual(pygame.image.tobytes(receiver.screen, "RGB"),
                         pygame.image.tobytes(preview.screen, "RGB"))

    def test_layout_fits_resizes_and_makes_room_for_area2(self):
        for preview in (False, True):
            engine = LoRaQEngine(pygame.Surface((800, 633 if preview else 450)),
                                 show_telemetry=preview, station_id="W" * 16, ack_slot=15)
            for size in ((320, 240), (640, 510), (800, 633), (1280, 870), (640, 960)):
                with self.subTest(preview=preview, size=size):
                    engine.resize(pygame.Surface(size))
                    engine.render()
                    self.assertEqual(len(engine._startup_surfaces), 5)
                    previous_bottom = 0
                    for surface, rect in engine._startup_surfaces:
                        self.assertTrue(engine.screen.get_rect().contains(rect), rect)
                        self.assertGreaterEqual(rect.top, previous_bottom)
                        previous_bottom = rect.top + surface.get_bounding_rect().bottom
                    engine.apply_records([Record("D", "2", text="STANDBY")])
                    engine.render()
                    self.assertTrue(engine._startup_active)
                    self.assertLessEqual(engine._startup_surfaces[-1][1].bottom,
                                         engine.area2_top_bound)

    def test_link_status_does_not_move_or_resize_startup_graphics(self):
        for preview in (False, True):
            for size in ((320, 240), (800, 633), (1280, 870)):
                with self.subTest(preview=preview, size=size):
                    engine = LoRaQEngine(pygame.Surface(size), show_telemetry=preview,
                                         station_id="CAB_1", ack_slot=3)
                    for area2 in (False, True):
                        if area2:
                            engine.apply_records([Record("D", "2", text="STANDBY")])
                        engine.render()
                        initial = [rect.copy() for _, rect in engine._startup_surfaces[:4]]
                        for connected in (False, True, False, True):
                            engine.set_link_status(connected)
                            engine.render()
                            self.assertEqual(
                                [rect for _, rect in engine._startup_surfaces[:4]], initial)

    def test_status_updates_cache_and_cue_transition(self):
        engine = LoRaQEngine(pygame.Surface((800, 450)))
        engine.set_link_status(False, "unplugged")
        engine.render()
        status_surface = engine._startup_surfaces[-1][0]
        self.assertIn((*COLOUR_MAP["R"], 255), {
            tuple(status_surface.get_at((x, y)))
            for x in range(status_surface.get_width())
            for y in range(status_surface.get_height())
        })
        cached = engine._startup_surfaces
        engine.render()
        self.assertIs(engine._startup_surfaces, cached)
        engine.set_link_status(True)
        engine.render()
        self.assertEqual(len(engine._startup_surfaces), 4)
        engine.apply_records([Record("D", "1", text="GO")])
        engine.render()
        self.assertFalse(engine._startup_active)
        self.assertEqual(engine.screen.get_at((0, 0))[:3], AREA1_BG)
        self.assertFalse(engine.set_link_status(False, "unplugged"))
        self.assertEqual(engine.state["area1_text"], "GO")

    def test_alert_preserves_startup_and_flashes_logo_and_text(self):
        engine = LoRaQEngine(pygame.Surface((800, 450)))
        with patch("loraq_gfx_engine.time.monotonic", return_value=100.0):
            engine.apply_records([Record("F", "0", effect_id=1)])
            engine.render()
        self.assertTrue(engine._startup_active)
        self.assertEqual(engine.screen.get_at((0, 0))[:3], COLOUR_MAP["W"])
        logo = engine._startup_surfaces[0][0]
        self.assertEqual(logo.get_at((logo.get_width() // 2, logo.get_height() // 2))[:3],
                         (0, 0, 0))
        with patch("loraq_gfx_engine.time.monotonic", return_value=103.0):
            engine.render()
        self.assertEqual(engine.screen.get_at((0, 0))[:3], STARTUP_BG)
        for area in ("1", "0"):
            engine = LoRaQEngine(pygame.Surface((800, 450)))
            engine.apply_records([Record("C", area)])
            engine.render()
            self.assertFalse(engine._startup_active)
            self.assertEqual(engine.screen.get_at((0, 0))[:3], AREA1_BG)


class BrandingTests(unittest.TestCase):
    def test_native_and_web_backgrounds_match(self):
        self.assertEqual(AREA1_BG, (9, 27, 88))
        self.assertEqual(STARTUP_BG, AREA1_BG)
        css = (ROOT / "web" / "loraq" / "static" / "viewer.css").read_text()
        self.assertIn("--area1-bg: #091b58;", css)
        php = (ROOT / "web" / "loraq" / "index.php").read_text()
        self.assertIn("'background_color' => '#091b58'", php)
        self.assertIn("'theme_color' => '#091b58'", php)

    def test_all_cue_colours_retain_large_text_contrast(self):
        def luminance(rgb):
            values = [v / 255 for v in rgb]
            linear = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4
                      for v in values]
            return sum(v * weight for v, weight in zip(linear, (0.2126, 0.7152, 0.0722)))

        background = luminance(AREA1_BG)
        for code, colour in COLOUR_MAP.items():
            with self.subTest(colour=code):
                self.assertGreaterEqual((luminance(colour) + 0.05) / (background + 0.05), 3)

    def test_generated_png_sizes_and_web_copies_match(self):
        icons = ROOT / "packaging" / "icons"
        for size in (16, 24, 32, 48, 64, 96, 128, 180, 192, 256, 512, 1024):
            file = icons / f"icon-{size}.png"
            expected = create_app_icon(size)
            image = pygame.image.load(str(file))
            self.assertEqual(image.get_size(), (size, size))
            self.assertEqual(pygame.image.tobytes(image, "RGBA"),
                             pygame.image.tobytes(expected, "RGBA"))
        viewer = ROOT / "web" / "loraq" / "static"
        for size in (180, 192, 512):
            self.assertEqual((viewer / f"icon-{size}.png").read_bytes(),
                             (icons / f"icon-{size}.png").read_bytes())
        self.assertEqual((viewer / "icon.svg").read_bytes(), (icons / "lq.svg").read_bytes())

    def test_demo_screenshots_have_distinct_cues_and_are_repeatable(self):
        spec = importlib.util.spec_from_file_location(
            "render_screenshots", ROOT / "packaging" / "render-screenshots.py")
        renderer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(renderer)
        pygame.font.init()
        self.addCleanup(pygame.font.quit)
        receiver, rx = renderer.cue_display(False)
        transmitter, tx = renderer.cue_display(True)
        self.assertEqual(receiver.get_size(), (800, 450))
        self.assertEqual(transmitter.get_size(), (800, 633))
        self.assertNotEqual(rx.state["area1_text"], tx.state["area1_text"])
        self.assertEqual(receiver.get_at((0, 0))[:3], AREA1_BG)
        no_ack = next(station for station in tx.stations if "NO ACK" in station[3])
        self.assertEqual(no_ack[2], "NO_RESPONSE")
        self.assertEqual(STATION_STATE_COLOURS[no_ack[2]], (255, 50, 50))
        stats = transmitter.subsurface(tx.stats_rect)
        self.assertIn((255, 50, 50), {
            tuple(stats.get_at((x, y)))[:3]
            for x in range(stats.get_width()) for y in range(stats.get_height())
        })
        self.assertEqual(pygame.image.tobytes(receiver, "RGB"),
                         pygame.image.tobytes(renderer.cue_display(False)[0], "RGB"))

    def test_manifest_icons_exist_at_the_declared_sizes(self):
        php = (ROOT / "web" / "loraq" / "index.php").read_text()
        sizes = re.findall(r"static/icon-(\d+)\.png', 'sizes' => '(\d+)x(\d+)'", php)
        self.assertEqual(len(sizes), 2)
        for name, width, height in sizes:
            image = pygame.image.load(str(ROOT / "web" / "loraq" / "static" / f"icon-{name}.png"))
            self.assertEqual(image.get_size(), (int(width), int(height)))


if __name__ == "__main__":
    unittest.main()
