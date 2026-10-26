import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from loraq_config import load_settings


class PackagedConfigTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name).resolve() / "Operator's show files"
        self.folder.mkdir()
        self.config = self.folder / "loraq.ini"
        shutil.copy2(Path(__file__).resolve().parents[1] / "loraq.ini", self.config)
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def test_source_uses_module_directory(self):
        with patch("loraq_config.__file__", str(self.folder / "loraq_config.py")), \
                patch.object(sys, "frozen", False, create=True):
            self.assertEqual(load_settings().path, self.config)

    def test_standalone_uses_executable_directory(self):
        for platform in ("win32", "linux", "darwin"):
            for app_name in ("LoRaQ-transmitter", "LoRaQ-display", "LoRaQ-adapter-configure"):
                with self.subTest(platform=platform, app=app_name), \
                    patch.object(sys, "frozen", True, create=True), \
                    patch.object(sys, "platform", platform), \
                    patch.object(sys, "executable", str(self.folder / app_name)):
                    self.assertEqual(load_settings().path, self.config)

    def test_mac_app_uses_outer_bundle_directory(self):
        for app_name in ("LoRaQ-transmitter", "LoRaQ-display", "LoRaQ-adapter-configure"):
            app = self.folder / f"{app_name}.app"
            for relative in (("MacOS", app_name), ("Resources", "application", app_name)):
                executable = app.joinpath("Contents", *relative)
                with self.subTest(app=app_name, relative=relative), \
                    patch.object(sys, "frozen", True, create=True), \
                    patch.object(sys, "platform", "darwin"), \
                    patch.object(sys, "executable", str(executable)):
                    self.assertEqual(load_settings().path, self.config)

    def test_explicit_path_and_environment_override_packaged_path(self):
        with patch.object(sys, "frozen", True, create=True), \
                patch.object(sys, "executable", str(self.folder / "missing" / "transmitter")), \
                patch.dict(os.environ, {"LORAQ_CONFIG": str(self.config)}):
            self.assertEqual(load_settings().path, self.config)
            os.environ["LORAQ_CONFIG"] = str(self.folder / "missing.ini")
            self.assertEqual(load_settings(self.config).path, self.config)
            with self.assertRaisesRegex(ValueError, "Configuration file not found"):
                load_settings()

    def test_missing_external_config_does_not_use_source_defaults(self):
        self.config.unlink()
        with patch.object(sys, "frozen", True, create=True), \
                patch.object(sys, "executable", str(self.folder / "LoRaQ-transmitter")):
            with self.assertRaisesRegex(ValueError, "Configuration file not found") as error:
                load_settings()
            self.assertIn(str(self.config), str(error.exception))


if __name__ == "__main__":
    unittest.main()
