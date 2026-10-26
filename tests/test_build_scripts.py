import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ShellScriptFormatTests(unittest.TestCase):
    def test_shell_scripts_use_unix_line_endings(self):
        for script in (ROOT / "packaging").rglob("*.sh"):
            with self.subTest(script=script.name):
                contents = script.read_bytes()
                self.assertTrue(contents.startswith(b"#!/bin/sh\n"))
                self.assertNotIn(b"\r", contents)


@unittest.skipUnless(os.name == "nt" and shutil.which("powershell"), "Windows PowerShell is required")
class WindowsBuildScriptTests(unittest.TestCase):
    def run_build_loop(self, fail_app=""):
        command = r"""
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($env:BUILD_SCRIPT, [ref]$tokens, [ref]$errors)
if ($errors.Count -gt 0) { throw 'Build script parse failed' }
$assignment = $ast.Find({
    param($node)
    $node -is [System.Management.Automation.Language.AssignmentStatementAst] -and $node.Left.Extent.Text -eq '$apps'
}, $true)
$loop = $ast.Find({
    param($node)
    $node -is [System.Management.Automation.Language.ForEachStatementAst] -and $node.Body.Extent.Text.Contains('-m PyInstaller')
}, $true)
if (-not $assignment -or -not $loop) { throw 'Build loop not found' }
$script:calls = New-Object System.Collections.ArrayList
function Invoke-TestPython {
    [void]$script:calls.Add(@($args))
    $global:LASTEXITCODE = 0
    if ($args[$args.IndexOf('--name') + 1] -eq $env:FAIL_APP) { $global:LASTEXITCODE = 7 }
}
$Python = 'Invoke-TestPython'; $icon = 'test.ico'; $artwork = 'test assets'
. ([scriptblock]::Create($assignment.Extent.Text))
$failure = ''
try { & ([scriptblock]::Create($loop.Extent.Text)) } catch { $failure = $_.Exception.Message }
[pscustomobject]@{ calls = @($script:calls); failure = $failure } | ConvertTo-Json -Compress -Depth 5
"""
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            env=dict(os.environ, BUILD_SCRIPT=str(ROOT / "packaging" / "build-windows.ps1"),
                     FAIL_APP=fail_app),
            text=True, capture_output=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_builds_all_three_with_icons_and_assets(self):
        result = self.run_build_loop()
        self.assertEqual(result["failure"], "")
        self.assertEqual(len(result["calls"]), 3)
        for args, app in zip(result["calls"],
                             ("LoRaQ-transmitter", "LoRaQ-display", "LoRaQ-adapter-configure")):
            self.assertEqual(args[args.index("--name") + 1], app)
            self.assertEqual(args[-1], app + ".py")
            self.assertEqual(args[args.index("--add-data") + 1], "test assets;assets")
            self.assertEqual(args[args.index("--icon") + 1], "test.ico")
            self.assertIn("--onefile", args)
            self.assertIn("--console", args)

    def test_failure_stops_subsequent_builds(self):
        result = self.run_build_loop("LoRaQ-display")
        self.assertEqual(len(result["calls"]), 2)
        self.assertEqual(result["failure"], "LoRaQ-display build failed.")


@unittest.skipUnless(shutil.which("sh"), "a POSIX shell is not installed")
class BuildScriptTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve() / "Operator's build files"
        packaging = self.root / "packaging"
        packaging.mkdir(parents=True)
        for platform in ("macos", "linux"):
            shutil.copy2(ROOT / "packaging" / f"build-{platform}.sh", packaging)
        (self.root / "loraq.ini").write_text("station settings", encoding="utf-8")
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.write_script(self.bin / "uname", '#!/bin/sh\nprintf "%s\\n" "$FAKE_OS"\n')
        self.write_script(self.bin / "objdump", "#!/bin/sh\nexit 0\n")
        interpreter = self.bin / "test-python"
        self.write_script(interpreter, """#!/bin/sh
printf '%s\\n' "$@" >> "$LOG"
if [ "$1" = "-c" ]; then
    case "$2" in
        *sys.base_prefix*)
            exit "${ENV_PREFIX_STATUS:-0}"
            ;;
        *find_spec*)
            if [ "${MISSING_PYINSTALLER:-0}" = "1" ]; then
                case "$0" in
                    */linux-env/bin/python) test -f "$0.installed"; exit $? ;;
                    *) exit 1 ;;
                esac
            fi
            ;;
        *"import pygame, serial.tools.list_ports"*)
            exit "${ENV_IMPORT_STATUS:-0}"
            ;;
    esac
    exit "${IMPORT_STATUS:-0}"
fi
if [ "$1" = "-m" ]; then
    if [ "$2" = "venv" ]; then
        if [ "${VENV_STATUS:-0}" != "0" ]; then exit "$VENV_STATUS"; fi
        target="$3"
        if [ "$3" = "--system-site-packages" ]; then target="$4"; fi
        mkdir -p "$target/bin"
        touch "$target/pyvenv.cfg"
        cp "$0" "$target/bin/python"
        chmod +x "$target/bin/python"
        exit 0
    fi
    if [ "$2" = "pip" ]; then
        if [ "${PIP_STATUS:-0}" != "0" ]; then exit "$PIP_STATUS"; fi
        touch "$0.installed"
        exit 0
    fi
    if [ "${BUILD_STATUS:-0}" != "0" ]; then exit "$BUILD_STATUS"; fi
    name=""
    while [ "$#" -gt 0 ]; do
        if [ "$1" = "--name" ]; then shift; name="$1"; fi
        shift
    done
    if [ "${FAIL_APP:-}" = "$name" ]; then exit 7; fi
    mkdir -p dist/linux
    touch "dist/linux/$name"
fi
""")
        self.env = dict(os.environ, PYTHON=interpreter.as_posix(),
                        LOG=(self.root / "calls.log").as_posix(),
                        TEST_BIN=self.bin.as_posix())

    def write_script(self, path, content):
        with path.open("w", encoding="utf-8", newline="\n") as output:
            output.write(content)
        path.chmod(0o755)

    def run_script(self, platform, **environment):
        env = dict(self.env, FAKE_OS="Darwin" if platform == "macos" else "Linux")
        env.update(environment)
        return subprocess.run(
            ["sh", "-c",
             'PATH="$(cd "$TEST_BIN" && pwd):$PATH"; export PATH; exec sh "$1"',
             "test-build", str(self.root / "packaging" / f"build-{platform}.sh")],
            cwd=self.bin, env=env, capture_output=True, text=True, timeout=15,
        )

    def test_linux_builds_from_any_directory_and_preserves_config(self):
        result = self.run_script("linux")
        self.assertEqual(result.returncode, 0, result.stderr)
        config = self.root / "dist" / "linux" / "loraq.ini"
        self.assertEqual(config.read_text(), "station settings")
        config.write_text("operator settings", encoding="utf-8")
        result = self.run_script("linux")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(config.read_text(), "operator settings")
        calls = (self.root / "calls.log").read_text()
        self.assertIn("serial.tools.list_ports", calls)
        self.assertIn("--onefile", calls)
        self.assertIn("/assets:assets", calls)
        self.assertNotIn("--onedir", calls)
        for app in ("LoRaQ-transmitter", "LoRaQ-display", "LoRaQ-adapter-configure"):
            self.assertTrue((config.parent / app).is_file())
            self.assertEqual(calls.count(f"\n--name\n{app}\n"), 2)
            self.assertEqual(calls.count(f"/{app}.py\n"), 2)
        self.assertIn("--clean", calls)
        self.assertNotIn("--system-site-packages", calls)
        self.assertNotIn("\npip\n", calls)

    def test_missing_pyinstaller_creates_and_reuses_build_environment(self):
        result = self.run_script("linux", MISSING_PYINSTALLER="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = (self.root / "calls.log").read_text()
        self.assertIn("--system-site-packages", calls)
        self.assertIn("pip\ninstall\npyinstaller", calls)
        self.assertTrue((self.root / "dist" / "linux" / "LoRaQ-display").is_file())
        result = self.run_script("linux", MISSING_PYINSTALLER="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = (self.root / "calls.log").read_text()
        self.assertEqual(calls.count("--system-site-packages"), 1)
        self.assertEqual(calls.count("pip\ninstall\npyinstaller"), 1)

    def test_environment_setup_failures_stop_build(self):
        for failure, message in (("VENV_STATUS", "python3-venv"),
                                 ("PIP_STATUS", "installation failed"),
                                 ("ENV_IMPORT_STATUS", "runtime dependencies")):
            with self.subTest(failure=failure):
                environment = self.root / "build" / "linux-env"
                if environment.exists():
                    shutil.rmtree(environment)
                result = self.run_script("linux", MISSING_PYINSTALLER="1",
                                         **{failure: "1"})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)
                self.assertFalse((self.root / "dist").exists())

    def test_invalid_existing_build_environment_is_not_overwritten(self):
        environment = self.root / "build" / "linux-env"
        environment.mkdir(parents=True)
        marker = environment / "keep"
        marker.write_text("existing files", encoding="utf-8")
        result = self.run_script("linux", MISSING_PYINSTALLER="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Invalid build environment", result.stderr)
        self.assertEqual(marker.read_text(), "existing files")

    def test_system_python_in_existing_build_environment_is_rejected_before_pip(self):
        environment = self.root / "build" / "linux-env"
        (environment / "bin").mkdir(parents=True)
        shutil.copy2(self.bin / "test-python", environment / "bin" / "python")
        config = environment / "pyvenv.cfg"
        for configured in (False, True):
            with self.subTest(configured=configured):
                if configured:
                    config.write_text("broken environment", encoding="utf-8")
                result = self.run_script("linux", MISSING_PYINSTALLER="1",
                                         ENV_PREFIX_STATUS="1")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Invalid build environment", result.stderr)
                self.assertIn("Move it aside", result.stderr)
                calls = (self.root / "calls.log").read_text()
                self.assertNotIn("\npip\n", calls)
                self.assertNotIn("--onefile", calls)
                self.assertTrue((environment / "bin" / "python").is_file())

    def test_failed_build_does_not_copy_config(self):
        result = self.run_script("linux", BUILD_STATUS="7")
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertFalse((self.root / "dist").exists())

    def test_failure_of_second_application_stops_remaining_builds(self):
        result = self.run_script("linux", FAIL_APP="LoRaQ-display")
        self.assertEqual(result.returncode, 7, result.stderr)
        output = self.root / "dist" / "linux"
        self.assertTrue((output / "LoRaQ-transmitter").is_file())
        self.assertFalse((output / "LoRaQ-adapter-configure").exists())
        self.assertFalse((output / "loraq.ini").exists())

    def test_old_folder_build_is_preserved_and_rejected(self):
        for app in ("LoRaQ-transmitter", "LoRaQ-display", "LoRaQ-adapter-configure"):
            with self.subTest(app=app):
                old = self.root / "dist" / "linux" / app
                old.mkdir(parents=True)
                config = old / "loraq.ini"
                config.write_text("old operator settings", encoding="utf-8")
                result = self.run_script("linux")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Move it aside", result.stderr)
                self.assertEqual(config.read_text(), "old operator settings")
                self.assertNotIn("--onefile", (self.root / "calls.log").read_text())
                shutil.rmtree(old)

    def test_mac_dispatches_to_existing_builder(self):
        result = self.run_script("macos")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("/packaging/macos/build.py", (self.root / "calls.log").read_text())

    def test_mac_creates_isolated_environment_and_reuses_it(self):
        for _ in range(2):
            result = self.run_script("macos")
            self.assertEqual(result.returncode, 0, result.stderr)
        calls = (self.root / "calls.log").read_text()
        self.assertEqual(calls.count("\nvenv\n"), 1)
        self.assertNotIn("--system-site-packages", calls)
        self.assertEqual(calls.count("--only-binary=pygame-ce"), 2)
        self.assertEqual(calls.count("pip\ninstall\n--upgrade\npip"), 2)
        self.assertLess(calls.index("pip\ninstall\n--upgrade\npip"),
                        calls.index("--only-binary=pygame-ce"))
        self.assertIn("/packaging/requirements.txt", calls)
        self.assertTrue((self.root / "build" / "macos-env" / "bin" / "python").is_file())

    def test_mac_setup_failure_prevents_packaging(self):
        for failure in ("VENV_STATUS", "PIP_STATUS"):
            with self.subTest(failure=failure):
                environment = self.root / "build" / "macos-env"
                if environment.exists():
                    shutil.rmtree(environment)
                result = self.run_script("macos", **{failure: "1"})
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("/packaging/macos/build.py",
                                 (self.root / "calls.log").read_text())
                if failure == "PIP_STATUS":
                    self.assertIn("Could not update pip", result.stderr)
                    self.assertNotIn("--only-binary=pygame-ce",
                                     (self.root / "calls.log").read_text())

    def test_import_failures_stop_both_builds(self):
        for platform in ("linux", "macos"):
            with self.subTest(platform=platform):
                result = self.run_script(platform, IMPORT_STATUS="1")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("environment", result.stderr)
                self.assertFalse((self.root / "dist").exists())

    def test_wrong_platform_is_rejected(self):
        for platform in ("linux", "macos"):
            with self.subTest(platform=platform):
                result = self.run_script(platform, FAKE_OS="WrongOS")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Build", result.stderr)
                self.assertFalse((self.root / "calls.log").exists())

    def test_missing_objdump_stops_before_python_setup(self):
        (self.bin / "objdump").unlink()
        # Limit PATH to the fixtures so an installed objdump cannot mask the check.
        result = subprocess.run(
            ["sh", "-c",
             'PATH="$(cd "$TEST_BIN" && pwd)"; export PATH; '
             'exec /bin/sh "$1"',
             "test-build", str(self.root / "packaging" / "build-linux.sh")],
            cwd=self.bin,
            env=dict(self.env, FAKE_OS="Linux"),
            capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("sudo apt install binutils", result.stderr)
        self.assertFalse((self.root / "calls.log").exists())


if __name__ == "__main__":
    unittest.main()
