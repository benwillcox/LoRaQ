import importlib.util
import os
import plistlib
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


BUILD_SCRIPT = Path(__file__).resolve().parents[1] / "packaging" / "macos" / "build.py"
spec = importlib.util.spec_from_file_location("macos_build", BUILD_SCRIPT)
assert spec is not None and spec.loader is not None
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)


class MacPackagingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "Operator's show files"
        self.root.mkdir()
        (self.root / "loraq.ini").write_text("station configuration", encoding="utf-8")
        root_patch = patch.object(build, "ROOT", self.root)
        root_patch.start()
        self.addCleanup(root_patch.stop)

    def fake_build_command(self, command, **kwargs):
        if command[0] == "/usr/bin/iconutil":
            Path(command[-1]).write_bytes(b"test icon")
        else:
            self.assertIn("--console", command)
            self.assertIn("--onedir", command)
            self.assertEqual(
                command[command.index("--add-data") + 1],
                str(self.root / "assets") + os.pathsep + "assets",
            )
            self.assertEqual(kwargs["cwd"], self.root)
            app_name = command[command.index("--name") + 1]
            self.assertIn(app_name, build.APP_NAMES)
            self.assertEqual(command[-1], str(self.root / f"{app_name}.py"))
            payload = Path(command[command.index("--distpath") + 1]) / app_name
            payload.mkdir(parents=True)
            (payload / app_name).write_text("test executable", encoding="utf-8")
            (payload / "_internal").mkdir()
            (payload / "_internal" / "library").write_bytes(b"test library")
        self.assertTrue(kwargs["check"])

    def run_build(self):
        with patch.object(build.sys, "platform", "darwin"), \
                patch.object(build.importlib.util, "find_spec", return_value=object()), \
                patch.object(build, "create_iconset") as iconset, \
                patch.object(build.subprocess, "run", side_effect=self.fake_build_command) as run:
            build.main()
            iconset.assert_called_once()
            self.assertEqual(run.call_count, 4)
            self.assertEqual(
                [call.args[0][call.args[0].index("--name") + 1] for call in run.call_args_list[1:]],
                list(build.APP_NAMES))

    def test_build_layout_metadata_and_launcher(self):
        self.run_build()
        output = self.root / "dist" / "LoRaQ"
        identifiers = {
            "LoRaQ-transmitter": "org.loraq.transmitter",
            "LoRaQ-display": "org.loraq.display",
            "LoRaQ-adapter-configure": "org.loraq.adapter-configure",
        }
        for app_name in build.APP_NAMES:
            with self.subTest(app=app_name):
                contents = output / f"{app_name}.app" / "Contents"
                with (contents / "Info.plist").open("rb") as source:
                    metadata = plistlib.load(source)
                self.assertEqual(metadata["CFBundlePackageType"], "APPL")
                self.assertEqual(metadata["CFBundleIconFile"], "LoRaQ.icns")
                self.assertEqual(metadata["CFBundleName"], app_name)
                self.assertEqual(metadata["CFBundleIdentifier"], identifiers[app_name])
                launcher = contents / "MacOS" / metadata["CFBundleExecutable"]
                expected = BUILD_SCRIPT.with_name("launcher.sh").read_bytes().replace(b"\r\n", b"\n")
                self.assertEqual(launcher.read_bytes(), expected.replace(b"@APP_NAME@", app_name.encode()))
                self.assertNotIn(b"\r", launcher.read_bytes())
                terminal_command = contents / "MacOS" / "LoRaQ-terminal.command"
                self.assertEqual(terminal_command.read_bytes(),
                                 BUILD_SCRIPT.with_name("terminal.command").read_bytes().replace(b"\r\n", b"\n"))
                self.assertIn(b'/usr/bin/open -a Terminal "$launcher_dir/LoRaQ-terminal.command"',
                              launcher.read_bytes())
                self.assertNotIn(b"do script", launcher.read_bytes())
                self.assertNotIn("NSAppleEventsUsageDescription", metadata)
                self.assertTrue((contents / "Resources" / "LoRaQ.icns").is_file())
                payload = contents / "Resources" / "application"
                self.assertTrue((payload / app_name).is_file())
                self.assertTrue((payload / "_internal" / "library").is_file())
        self.assertEqual((output / "loraq.ini").read_text(), "station configuration")

    def test_existing_configuration_is_preserved(self):
        output = self.root / "dist" / "LoRaQ"
        output.mkdir(parents=True)
        (output / "loraq.ini").write_text("operator settings", encoding="utf-8")
        self.run_build()
        self.assertEqual((output / "loraq.ini").read_text(), "operator settings")

    def test_existing_app_is_not_overwritten(self):
        for app_name in build.APP_NAMES:
            with self.subTest(app=app_name):
                app = self.root / "dist" / "LoRaQ" / f"{app_name}.app"
                app.mkdir(parents=True)
                with patch.object(build.sys, "platform", "darwin"), \
                        patch.object(build.importlib.util, "find_spec", return_value=object()), \
                        patch.object(build.subprocess, "run") as run:
                    with self.assertRaisesRegex(SystemExit, "already exists"):
                        build.main()
                    run.assert_not_called()
                app.rmdir()

    def test_failed_build_does_not_publish_partial_set_or_change_config(self):
        output = self.root / "dist" / "LoRaQ"
        output.mkdir(parents=True)
        config = output / "loraq.ini"
        config.write_text("operator settings", encoding="utf-8")

        def fail_on_display(command, **kwargs):
            if "--name" in command and command[command.index("--name") + 1] == "LoRaQ-display":
                raise subprocess.CalledProcessError(7, command)
            self.fake_build_command(command, **kwargs)

        with patch.object(build.sys, "platform", "darwin"), \
                patch.object(build.importlib.util, "find_spec", return_value=object()), \
                patch.object(build, "create_iconset"), \
                patch.object(build.subprocess, "run", side_effect=fail_on_display):
            with self.assertRaises(subprocess.CalledProcessError):
                build.main()
        self.assertEqual(list(output.glob("*.app")), [])
        self.assertEqual(config.read_text(), "operator settings")

    def test_non_mac_build_is_rejected(self):
        with patch.object(build.sys, "platform", "win32"):
            with self.assertRaisesRegex(SystemExit, "cannot cross-compile"):
                build.main()

    def test_missing_dependencies_are_reported(self):
        with patch.object(build.sys, "platform", "darwin"), \
                patch.object(build.importlib.util, "find_spec", return_value=None):
            with self.assertRaisesRegex(SystemExit, "Missing build dependencies"):
                build.main()

    @unittest.skipUnless(shutil.which("sh"), "a POSIX shell is not installed")
    def test_terminal_session_preserves_input_logs_and_exit_status(self):
        for app_name, status in ((name, status) for name in build.APP_NAMES for status in (0, 7)):
            with self.subTest(app=app_name, status=status):
                payload = self.root / f"payload-{app_name}-{status}"
                payload.mkdir()
                executable = payload / app_name
                executable.write_text(
                    "#!/bin/sh\n"
                    "IFS= read -r command\n"
                    "printf 'Application received: %s\\n' \"$command\"\n"
                    f"exit {status}\n",
                    encoding="utf-8", newline="\n",
                )
                executable.chmod(0o755)
                icon = self.root / "icon.icns"
                icon.write_bytes(b"test icon")
                app = self.root / f"Test-{app_name}-{status}.app"
                build.assemble_app(app, payload, icon, app_name)
                launcher = app / "Contents" / "MacOS" / "LoRaQ-terminal.command"
                result = subprocess.run(
                    ["sh", str(launcher)],
                    input="help\n\n", text=True, capture_output=True,
                    cwd=self.root, timeout=10,
                )
                self.assertEqual(result.returncode, status, result.stderr)
                self.assertIn("Application received: help", result.stdout)
                self.assertIn(f"LoRaQ exited (status {status})", result.stdout)
                self.assertIn("Press Return", result.stdout)

    @unittest.skipUnless(importlib.util.find_spec("pygame"), "pygame is not installed")
    def test_iconset_has_required_sizes_and_adaptive_navy_amber_design(self):
        import pygame

        directory = self.root / "LoRaQ.iconset"
        build.create_iconset(directory)
        self.assertEqual(len(list(directory.glob("*.png"))), 10)
        for size in (16, 32, 128, 256, 512):
            for scale in (1, 2):
                suffix = "@2x" if scale == 2 else ""
                image = pygame.image.load(str(directory / f"icon_{size}x{size}{suffix}.png"))
                self.assertEqual(image.get_size(), (size * scale, size * scale))
                from loraq_icon import create_app_icon
                expected = pygame.transform.smoothscale(create_app_icon(size),
                                                       (size * scale, size * scale))
                self.assertEqual(pygame.image.tobytes(image, "RGBA"),
                                 pygame.image.tobytes(expected, "RGBA"))
        image = pygame.image.load(str(directory / "icon_512x512@2x.png"))
        self.assertEqual(image.get_at((512, 80)), pygame.Color("#091b58"))
        self.assertEqual(image.get_at((0, 0)).a, 0)
        self.assertGreater(sum(image.get_at((x, y)) == pygame.Color("white")
                               for x in range(200, 825, 8)
                               for y in range(200, 825, 8)), 100)


if __name__ == "__main__":
    unittest.main()
