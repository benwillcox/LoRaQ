import ctypes
import os
import io
import struct
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import pygame

import loraq_icon


class AppIconTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {"SDL_VIDEODRIVER": "dummy"})
        environment.start()
        self.addCleanup(environment.stop)
        pygame.display.init()
        pygame.font.init()
        self.addCleanup(pygame.display.quit)
        self.addCleanup(pygame.font.quit)

    def test_artwork_matches_packaged_icon(self):
        image = loraq_icon.create_app_icon()
        self.assertEqual(image.get_size(), (1024, 1024))
        self.assertEqual(image.get_at((512, 80)), pygame.Color("#091b58"))
        self.assertEqual(image.get_at((0, 0)).a, 0)
        self.assertGreater(sum(image.get_at((x, y)) == pygame.Color("white")
                               for x in range(200, 825, 8)
                               for y in range(200, 825, 8)), 100)
        self.assertGreater(sum(image.get_at((x, y)) == pygame.Color("#ffd166")
                               for x in range(200, 825, 8)
                               for y in range(200, 825, 8)), 50)

    def test_icon_can_be_set_before_window_creation(self):
        with patch.object(loraq_icon.sys, "platform", "linux"):
            loraq_icon.set_app_icon("LoRaQ.Receiver")
            pygame.display.set_mode((64, 64))
            loraq_icon.set_app_icon("LoRaQ.Receiver")

    def test_windows_icon_frames_match_shared_artwork(self):
        data = (loraq_icon.Path(loraq_icon.__file__).parent
                / "packaging" / "windows" / "loraq.ico").read_bytes()
        self.assertEqual(struct.unpack_from("<HHH", data), (0, 1, 7))
        for index, size in enumerate((16, 24, 32, 48, 64, 128, 256)):
            entry = struct.unpack_from("<BBBBHHII", data, 6 + index * 16)
            length, offset = entry[-2:]
            actual = pygame.image.load(io.BytesIO(data[offset:offset + length]))
            expected = loraq_icon.create_app_icon(size)
            self.assertEqual(pygame.image.tobytes(actual, "RGBA"),
                             pygame.image.tobytes(expected, "RGBA"))

    def test_artwork_switches_at_64_pixels(self):
        folder = loraq_icon.Path(loraq_icon.__file__).parent / "assets"
        for size in (16, 32, 48, 64, 128, 1024):
            with self.subTest(size=size):
                source = pygame.image.load(str(folder / ("lq.png" if size < 64 else "loraq.png")))
                expected = pygame.transform.smoothscale(source, (size, size))
                self.assertEqual(pygame.image.tobytes(loraq_icon.create_app_icon(size), "RGBA"),
                                 pygame.image.tobytes(expected, "RGBA"))

    def test_mac_and_linux_use_sdl_icon(self):
        for platform in ("darwin", "linux"):
            with self.subTest(platform=platform), \
                    patch.object(loraq_icon.sys, "platform", platform), \
                    patch.object(pygame.display, "set_icon") as set_icon:
                loraq_icon.set_app_icon("LoRaQ.Transmitter")
                self.assertIsInstance(set_icon.call_args.args[0], pygame.Surface)
                self.assertEqual(set_icon.call_args.args[0].get_width(),
                                 1024 if platform == "darwin" else 32)

    def test_source_installation_runs_without_packaging_folder(self):
        root = loraq_icon.Path(loraq_icon.__file__).parent
        with tempfile.TemporaryDirectory() as temporary:
            installation = loraq_icon.Path(temporary)
            for name in ("loraq_icon.py", "loraq_gfx_engine.py", "loraq_config.py",
                         "loraq_protocol.py", "loraq.ini"):
                shutil.copy2(root / name, installation)
            shutil.copytree(root / "assets", installation / "assets")
            result = subprocess.run(
                [sys.executable, "-I", "-c",
                 "import sys; sys.path.insert(0, sys.argv[1]); "
                 "import pygame; from loraq_icon import set_app_icon; "
                 "from loraq_gfx_engine import LoRaQEngine; "
                 "pygame.display.init(); pygame.font.init(); "
                 "set_app_icon('LoRaQ.Test'); "
                 "engine = LoRaQEngine(pygame.Surface((800, 450))); "
                 "engine.render(); assert len(engine._startup_surfaces) == 5; pygame.quit()",
                 str(installation)],
                cwd=installation, env=dict(os.environ, LORAQ_CONFIG=str(installation / "loraq.ini")),
                text=True, capture_output=True, timeout=15,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_windows_sets_identity_before_icon(self):
        events = []
        shell32 = Mock()
        shell32.SetCurrentProcessExplicitAppUserModelID.side_effect = (
            lambda app_id: events.append(("identity", app_id)) or 0
        )
        with patch.object(loraq_icon.sys, "platform", "win32"), \
                patch.object(ctypes, "WinDLL", return_value=shell32, create=True), \
                patch.object(pygame.display, "set_icon",
                             side_effect=lambda image: events.append(("icon", image))):
            loraq_icon.set_app_icon("LoRaQ.Transmitter")
        self.assertEqual(events[0], ("identity", "LoRaQ.Transmitter"))
        self.assertEqual(events[1][0], "icon")
        self.assertEqual(shell32.SetCurrentProcessExplicitAppUserModelID.argtypes,
                         [ctypes.c_wchar_p])

    def test_windows_identity_failure_is_logged_and_icon_is_still_set(self):
        shell32 = Mock()
        shell32.SetCurrentProcessExplicitAppUserModelID.return_value = -2147467259
        with patch.object(loraq_icon.sys, "platform", "win32"), \
                patch.object(ctypes, "WinDLL", return_value=shell32, create=True), \
                patch.object(pygame.display, "set_icon") as set_icon, \
                self.assertLogs("loraq.icon", level="WARNING") as logs:
            loraq_icon.set_app_icon("LoRaQ.Receiver")
        self.assertIn("0x80004005", logs.output[0])
        set_icon.assert_called_once()


if __name__ == "__main__":
    unittest.main()
