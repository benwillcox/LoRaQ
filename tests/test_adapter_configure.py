import importlib.util
import io
import os
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import loraq_protocol as protocol


SCRIPT = Path(__file__).resolve().parents[1] / "LoRaQ-adapter-configure.py"
spec = importlib.util.spec_from_file_location("adapter_configure", SCRIPT)
assert spec is not None and spec.loader is not None
configure = importlib.util.module_from_spec(spec)
spec.loader.exec_module(configure)


class AdapterDetectionTests(unittest.TestCase):
    def setUp(self):
        self.output = io.StringIO()
        stdout = redirect_stdout(self.output)
        stdout.__enter__()
        self.addCleanup(stdout.__exit__, None, None, None)
        self.serial = Mock()
        registers = configure.build_registers(configure.SETTINGS.radio)
        self.command = b"\xC0\x00" + bytes([len(registers)]) + registers
        self.serial.read_all.return_value = b"\xC1" + self.command[1:]
        for target, value in (
            (patch.dict(os.environ, {}, clear=True), None),
            (patch.object(protocol.os.path, "exists", return_value=False), None),
            (patch.object(protocol.serial.tools.list_ports, "comports", return_value=[]), None),
            (patch.object(configure.serial, "Serial", return_value=self.serial), "factory"),
            (patch.object(configure.time, "sleep"), None),
        ):
            result = target.start()
            self.addCleanup(target.stop)
            if value:
                setattr(self, value, result)

    def use_link(self, interface="auto", port=None):
        settings = replace(configure.SETTINGS,
                           link=replace(configure.SETTINGS.link, interface=interface, port=port))
        for module in (configure, protocol):
            settings_patch = patch.object(module, "SETTINGS", settings)
            settings_patch.start()
            self.addCleanup(settings_patch.stop)

    def assert_programs(self, port):
        self.assertEqual(protocol.find_lora_port(), port)
        self.assertEqual(configure.main(), 0, self.output.getvalue())
        self.factory.assert_called_once_with(port, configure.CONFIG_BAUD, timeout=1)
        self.serial.write.assert_called_once_with(self.command)
        self.serial.close.assert_called_once()

    def test_auto_detects_same_usb_adapter_as_runtime_on_each_platform(self):
        for device, description in (
            ("COM9", "Silicon Labs CP210x USB to UART Bridge"),
            ("/dev/cu.usbserial-123", "USB Serial"),
            ("/dev/ttyUSB0", "USB Serial"),
        ):
            with self.subTest(device=device), \
                    patch.object(protocol.serial.tools.list_ports, "comports", return_value=[
                        SimpleNamespace(device=device, description=description, hwid="USB")
                    ]):
                self.use_link()
                self.assert_programs(device)
                self.factory.reset_mock()
                self.serial.reset_mock()

    def test_usb_interface_uses_usb_detection(self):
        self.use_link("usb")
        with patch.object(protocol.serial.tools.list_ports, "comports", return_value=[
            SimpleNamespace(device="COM7", description="USB Serial", hwid="USB")
        ]):
            self.assert_programs("COM7")

    def test_auto_and_hat_detect_gpio_uart(self):
        for interface in ("auto", "hat"):
            with self.subTest(interface=interface), \
                    patch.object(protocol.os.path, "exists",
                                 side_effect=lambda path: path == "/dev/serial0"):
                self.use_link(interface)
                self.assert_programs("/dev/serial0")
                self.factory.reset_mock()
                self.serial.reset_mock()

    def test_explicit_port_and_environment_override_match_runtime(self):
        self.use_link(port="COM4")
        self.assert_programs("COM4")
        self.factory.reset_mock()
        self.serial.reset_mock()
        with patch.dict(os.environ, LORAQ_PORT="COM8"):
            self.assert_programs("COM8")

    def test_hat_interface_does_not_program_usb_adapter(self):
        self.use_link("hat")
        with patch.object(protocol.serial.tools.list_ports, "comports", return_value=[
            SimpleNamespace(device="COM7", description="USB Serial", hwid="USB")
        ]):
            self.assertEqual(configure.main(), 1)
        self.factory.assert_not_called()
        self.assertIn("COM7", self.output.getvalue())

    def test_serial_port_is_closed_after_io_failure(self):
        self.use_link(port="COM4")
        self.serial.write.side_effect = configure.serial.SerialException("disconnected")
        with self.assertRaises(configure.serial.SerialException):
            configure.main()
        self.serial.close.assert_called_once()

    def test_no_response_or_incorrect_echo_is_not_success(self):
        self.use_link(port="COM4")
        for response in (b"", b"\xC1\x00"):
            with self.subTest(response=response):
                self.serial.read_all.return_value = response
                self.assertEqual(configure.main(), 1)


class AdapterConsoleTests(unittest.TestCase):
    def test_windows_binary_waits_after_success_or_failure(self):
        for status in (0, 1):
            with self.subTest(status=status), \
                    patch.object(configure.sys, "platform", "win32"), \
                    patch.object(configure.sys, "frozen", True, create=True), \
                    patch.object(configure.sys.stdin, "isatty", return_value=True), \
                    patch.object(configure, "main", return_value=status), \
                    patch("builtins.input", return_value="") as prompt:
                self.assertEqual(configure.run(), status)
                prompt.assert_called_once()
                self.assertIn("Return", prompt.call_args.args[0])

    def test_serial_errors_are_reported_and_window_stays_open(self):
        for error in (configure.serial.SerialException("port busy"), OSError("unplugged")):
            with self.subTest(error=error), \
                    patch.object(configure.sys, "platform", "win32"), \
                    patch.object(configure.sys, "frozen", True, create=True), \
                    patch.object(configure.sys.stdin, "isatty", return_value=True), \
                    patch.object(configure, "main", side_effect=error), \
                    patch("builtins.input", return_value="") as prompt, \
                    redirect_stdout(io.StringIO()) as output:
                self.assertEqual(configure.run(), 1)
                self.assertIn(str(error), output.getvalue())
                prompt.assert_called_once()

    def test_other_launch_modes_do_not_add_a_pause(self):
        for platform, frozen, interactive in (
            ("win32", False, True),
            ("win32", True, False),
            ("darwin", True, True),
            ("linux", True, True),
        ):
            with self.subTest(platform=platform, frozen=frozen, interactive=interactive), \
                    patch.object(configure.sys, "platform", platform), \
                    patch.object(configure.sys, "frozen", frozen, create=True), \
                    patch.object(configure.sys.stdin, "isatty", return_value=interactive), \
                    patch.object(configure, "main", return_value=0), \
                    patch("builtins.input") as prompt:
                self.assertEqual(configure.run(), 0)
                prompt.assert_not_called()

    def test_closing_prompt_does_not_change_failure_status(self):
        for error in (EOFError, KeyboardInterrupt):
            with self.subTest(error=error), \
                    patch.object(configure.sys, "platform", "win32"), \
                    patch.object(configure.sys, "frozen", True, create=True), \
                    patch.object(configure.sys.stdin, "isatty", return_value=True), \
                    patch.object(configure, "main", return_value=1), \
                    patch("builtins.input", side_effect=error), \
                    redirect_stdout(io.StringIO()) as output:
                self.assertEqual(configure.run(), 1)
                self.assertIn("Console session closed", output.getvalue())

    def test_windows_binary_waits_after_configuration_validation_error(self):
        with patch.object(configure.sys, "platform", "win32"), \
                patch.object(configure.sys, "frozen", True, create=True), \
                patch.object(configure.sys.stdin, "isatty", return_value=True), \
                patch.object(configure, "main", side_effect=SystemExit("unsupported baud")), \
                patch("builtins.input", return_value="") as prompt:
            with self.assertRaisesRegex(SystemExit, "unsupported baud"):
                configure.run()
            prompt.assert_called_once()


if __name__ == "__main__":
    unittest.main()
