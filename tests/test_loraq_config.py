import configparser
import importlib.util
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from loraq_config import SETTINGS, load_settings

FAKE_SERIAL = """
class FakeSerial:
    def __init__(self, *args, **kwargs):
        self.is_open = True
        self.written = bytearray()
        self.buf = bytearray()

    def reset_input_buffer(self):
        self.buf.clear()

    def write(self, data):
        self.written += data
        self.buf += b'+OK\\r\\n'
        return len(data)

    def flush(self):
        pass

    @property
    def in_waiting(self):
        return len(self.buf)

    def read(self, size):
        out = bytes(self.buf[:size])
        del self.buf[:size]
        return out

    def close(self):
        self.is_open = False
"""


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.path = Path(self.temp_dir.name) / "station.ini"
        self.parser = configparser.ConfigParser(interpolation=None)
        self.parser.optionxform = str
        self.parser.read(SETTINGS.path, encoding="utf-8")

    def save(self):
        with self.path.open("w", encoding="utf-8") as output:
            self.parser.write(output)

    def run_configured(self, code):
        self.save()
        env = dict(os.environ, LORAQ_CONFIG=str(self.path), SDL_VIDEODRIVER="dummy")
        env.pop("LORAQ_STATION_ID", None)
        env.pop("LORAQ_STATION_SLOT", None)
        env.pop("LORAQ_PORT", None)
        return subprocess.run([sys.executable, "-c", code], env=env, text=True,
                              capture_output=True, cwd=Path(__file__).parent.parent, timeout=15)

    def test_loaded_values_match_ini(self):
        self.assertEqual(SETTINGS.transmitter.osc_port, self.parser.getint("transmitter", "osc_port"))
        self.assertEqual(SETTINGS.transmitter.retries, self.parser.getint("transmitter", "retries"))
        self.assertEqual(SETTINGS.transmitter.test_interval_s, 3.0)
        self.assertEqual(SETTINGS.transmitter.max_dwell_time, 0)
        self.assertEqual(SETTINGS.transmitter.preview_width, self.parser.getint("transmitter", "preview_width"))
        self.assertEqual(SETTINGS.transmitter.preview_height, self.parser.getint("transmitter", "preview_height"))
        self.assertEqual(SETTINGS.station.ack_slot, self.parser.getint("station", "ack_slot"))
        self.assertFalse(hasattr(SETTINGS.transmitter, "miss_alert_threshold"))
        self.assertFalse(self.parser.has_section("timing"))

    def test_osc_port_default_and_override(self):
        settings = load_settings(Path(__file__).parent.parent / "loraq.ini")
        self.assertEqual(settings.transmitter.osc_port, 53070)
        self.parser.set("transmitter", "osc_port", "54070")
        self.save()
        self.assertEqual(load_settings(self.path).transmitter.osc_port, 54070)

    def test_station_identity_is_mandatory(self):
        for option in ("station_id", "ack_slot"):
            original = self.parser.get("station", option)
            self.parser.remove_option("station", option)
            self.save()
            with self.subTest(option=option), self.assertRaises(ValueError):
                load_settings(self.path)
            self.parser.set("station", option, original)
        self.parser.remove_section("station")
        self.parser.set("receiver", "station_id", "OLD")
        self.parser.set("receiver", "ack_slot", "0")
        result = self.run_configured("import loraq_config")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("station", result.stderr)

    def test_station_identity_validation(self):
        for option, values in (("station_id", ("", "bad id", "A" * 17, "caf\u00e9")),
                               ("ack_slot", ("", "-1", "32", "1.0", "auto"))):
            original = self.parser.get("station", option)
            for value in values:
                self.parser.set("station", option, value)
                self.save()
                with self.subTest(option=option, value=value), self.assertRaisesRegex(ValueError, option):
                    load_settings(self.path)
            self.parser.set("station", option, original)
        self.parser.set("station", "ack_slot", "0")
        self.save()
        self.assertEqual(load_settings(self.path).station.ack_slot, 0)

    def test_receiver_tx_info_defaults_and_validation(self):
        self.parser.remove_option("receiver", "show_tx_info")
        self.save()
        self.assertTrue(load_settings(self.path).receiver.show_tx_info)
        self.parser.set("receiver", "show_tx_info", "false")
        self.save()
        settings = load_settings(self.path)
        self.assertFalse(settings.receiver.show_tx_info)
        self.assertFalse(hasattr(settings.station, "show_tx_info"))
        self.assertFalse(hasattr(settings.transmitter, "show_tx_info"))
        self.parser.set("receiver", "show_tx_info", "perhaps")
        self.save()
        with self.assertRaisesRegex(ValueError, "boolean"):
            load_settings(self.path)

    def test_station_diagnostic_log_defaults_off_and_validates(self):
        self.parser.remove_option("station", "diagnostic_log")
        self.save()
        self.assertFalse(load_settings(self.path).station.diagnostic_log)
        self.parser.set("station", "diagnostic_log", "true")
        self.save()
        settings = load_settings(self.path)
        self.assertTrue(settings.station.diagnostic_log)
        self.assertFalse(hasattr(settings.receiver, "diagnostic_log"))
        self.parser.set("station", "diagnostic_log", "sometimes")
        self.save()
        with self.assertRaisesRegex(ValueError, "boolean"):
            load_settings(self.path)

    def test_receiver_audio_defaults_and_validation(self):
        for option in ("audio_enabled", "audio_volume"):
            self.parser.remove_option("receiver", option)
        self.save()
        receiver = load_settings(self.path).receiver
        self.assertFalse(receiver.audio_enabled)
        self.parser.set("receiver", "audio_enabled", "true")
        self.parser.set("receiver", "audio_volume", "0.25")
        self.save()
        receiver = load_settings(self.path).receiver
        self.assertTrue(receiver.audio_enabled)
        self.assertFalse(hasattr(receiver, "audio_volume"))
        self.parser.set("receiver", "audio_enabled", "perhaps")
        self.save()
        with self.assertRaisesRegex(ValueError, "boolean"):
            load_settings(self.path)

    def test_web_push_defaults_and_validation(self):
        self.parser.remove_section("web_push")
        self.save()
        web = load_settings(self.path).web_push
        self.assertFalse(web.enabled)
        self.assertTrue(web.push_remote)

        self.parser.add_section("web_push")
        self.parser.set("web_push", "enabled", "true")
        self.save()
        with self.assertRaisesRegex(ValueError, "url"):
            load_settings(self.path)
        self.parser.set("web_push", "url", "https://example.com/loraq/")
        self.parser.set("web_push", "token", "abcdefghijklmnop_1234")
        self.save()
        web = load_settings(self.path).web_push
        self.assertTrue(web.enabled)
        self.assertEqual(web.url, "https://example.com/loraq")
        self.assertEqual(web.token, "abcdefghijklmnop_1234")

        for option, value in (("url", "ftp://example.com"), ("url", "example.com"), ("token", "short"),
                              ("token", "has space in it 123"), ("timeout_s", "0"), ("timeout_s", "61"),
                              ("max_queue", "5"), ("heartbeat_s", "1"), ("push_remote", "perhaps")):
            original = self.parser.get("web_push", option, fallback=None)
            self.parser.set("web_push", option, value)
            self.save()
            expected = "boolean" if option == "push_remote" else option
            with self.subTest(option=option, value=value), self.assertRaisesRegex(ValueError, expected):
                load_settings(self.path)
            if original is None:
                self.parser.remove_option("web_push", option)
            else:
                self.parser.set("web_push", option, original)

    def test_legacy_web_push_tests_setting_is_ignored(self):
        if not self.parser.has_section("web_push"):
            self.parser.add_section("web_push")
        self.parser.set("web_push", "push_tests", "true")
        self.save()
        self.assertFalse(hasattr(load_settings(self.path).web_push, "push_tests"))

    def test_transmitter_remote_audio_defaults_and_validation(self):
        self.parser.remove_option("transmitter", "audio_enabled")
        self.save()
        self.assertTrue(load_settings(self.path).transmitter.audio_enabled)
        self.parser.set("transmitter", "audio_enabled", "false")
        self.save()
        self.assertFalse(load_settings(self.path).transmitter.audio_enabled)
        self.parser.set("transmitter", "audio_enabled", "perhaps")
        self.save()
        with self.assertRaisesRegex(ValueError, "boolean"):
            load_settings(self.path)

    def test_preview_dimensions_are_configurable_and_default_for_older_configs(self):
        self.parser.remove_option("transmitter", "preview_width")
        self.parser.remove_option("transmitter", "preview_height")
        self.save()
        settings = load_settings(self.path)
        self.assertEqual((settings.transmitter.preview_width, settings.transmitter.preview_height), (640, 360))

        self.parser.set("transmitter", "preview_width", "1280")
        self.parser.set("transmitter", "preview_height", "720")
        self.save()
        settings = load_settings(self.path)
        self.assertEqual((settings.transmitter.preview_width, settings.transmitter.preview_height), (1280, 720))

        for option, value in (("preview_width", "399"), ("preview_height", "319")):
            self.parser.set("transmitter", "preview_width", "1280")
            self.parser.set("transmitter", "preview_height", "720")
            self.parser.set("transmitter", option, value)
            self.save()
            with self.subTest(option=option), self.assertRaisesRegex(ValueError, option):
                load_settings(self.path)

    def test_max_dwell_time_is_optional_and_validated(self):
        self.parser.remove_option("transmitter", "max_dwell_time")
        self.save()
        self.assertEqual(load_settings(self.path).transmitter.max_dwell_time, 0)
        self.parser.set("transmitter", "max_dwell_time", "250.5")
        self.save()
        self.assertEqual(load_settings(self.path).transmitter.max_dwell_time, 250.5)
        for invalid in ("-1", "nan", "inf"):
            self.parser.set("transmitter", "max_dwell_time", invalid)
            self.save()
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, "max_dwell_time"):
                load_settings(self.path)

    def test_osc_forward_target_is_optional_and_validated(self):
        for option in ("osc_forward_ip", "osc_forward_port"):
            self.parser.remove_option("transmitter", option)
        self.save()
        settings = load_settings(self.path).transmitter
        self.assertEqual((settings.osc_forward_ip, settings.osc_forward_port), (None, None))

        self.parser.set("transmitter", "osc_forward_ip", "192.168.1.50")
        self.parser.set("transmitter", "osc_forward_port", "53000")
        self.save()
        settings = load_settings(self.path).transmitter
        self.assertEqual((settings.osc_forward_ip, settings.osc_forward_port), ("192.168.1.50", 53000))

        for ip, port in (("", "53000"), ("192.168.1.50", ""), ("192.168.1.50", "70000"), ("bad host", "53000")):
            self.parser.set("transmitter", "osc_forward_ip", ip)
            self.parser.set("transmitter", "osc_forward_port", port)
            self.save()
            with self.subTest(ip=ip, port=port), self.assertRaisesRegex(ValueError, "osc_forward"):
                load_settings(self.path)

    def test_custom_values_change_on_air_frames_and_timing(self):
        self.parser.set("radio", "spreading_factor", "9")
        self.parser.set("radio", "bandwidth_khz", "125")
        self.parser.set("radio", "coding_rate", "1")
        self.parser.set("radio", "max_frame_bytes", "200")
        self.parser.set("transmitter", "retries", "4")
        self.parser.set("station", "station_id", "CAB_1")
        self.parser.set("station", "ack_slot", "2")
        self.parser.set("expected_stations", "CAB_1", "2")
        result = self.run_configured("""
import importlib.util
import sys
from loraq_config import SETTINGS
import loraq_protocol as protocol
from loraq_protocol import Record

spec = importlib.util.spec_from_file_location('tx', 'LoRaQ-transmitter.py')
tx = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = tx
spec.loader.exec_module(tx)

class Link:
    connected = True
    rx_bytes = 0
    def __init__(self): self.written = []
    def ensure_open(self): return True
    def write(self, data): self.written.append(protocol.decode_frame(data)); return True

link = Link()
events = []
transmitter = tx.Transmitter(link, on_ui=events.append)
records = [Record('D', '1', text='GO')]
transmitter._transmit([tx.TxItem('cue', records)])
assert len(link.written) == 5
assert [f.burst for f in link.written] == [1, 2, 3, 4, 5]
assert all(f.bursts == 5 for f in link.written)
assert all(f.station == 'CAB_1' for f in link.written)
assert abs([event for event in events if event[0] == 'duty'][-1][3]
           - protocol.lora_toa_s(len(protocol.encode_cue('CAB_1', transmitter.session, 1, 5, 5, records))) * 1000) < 0.001
transmitter._transmit([tx.TxItem('test', records)])
assert len(link.written) == 6 and link.written[-1].bursts == 1
assert protocol.LORA_SF == 9 and protocol.ACK_SLOT_S > 0.15
assert protocol.ack_window_s(50, 3) == (protocol.lora_toa_s(50) + protocol.ACK_GUARD_S
                                         + 3 * protocol.ACK_SLOT_S + protocol.ACK_WINDOW_MARGIN_S)
assert transmitter.tracker.slot_count() == 4
assert SETTINGS.station.station_id == 'CAB_1' and SETTINGS.station.ack_slot == 2
assert protocol.APP_VERSION == '1.1'

spec = importlib.util.spec_from_file_location('rx', 'LoRaQ-display.py')
rx = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = rx
spec.loader.exec_module(rx)
sys.argv = ['LoRaQ-display.py']
import os
os.environ['LORAQ_STATION_ID'] = 'OVERRIDE'
os.environ['LORAQ_STATION_SLOT'] = '31'
assert (rx.parse_args().station_id, rx.parse_args().slot) == ('CAB_1', 2)
for flags in (['--station-id', 'STAGE_LEFT'], ['--slot', '3']):
    sys.argv = ['LoRaQ-display.py', *flags]
    try: rx.parse_args()
    except SystemExit as exc: assert exc.code == 2
    else: raise AssertionError('identity override accepted')
""")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_invalid_retry_count_fails_at_startup(self):
        self.parser.set("transmitter", "retries", "9")
        result = self.run_configured("import loraq_protocol")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("[transmitter] retries", result.stderr)

    def test_hat_interface_uses_configured_port(self):
        self.parser.set("link", "interface", "hat")
        self.parser.set("link", "port", "/dev/serial0")
        result = self.run_configured(textwrap.dedent("""
            from loraq_config import SETTINGS
            from loraq_protocol import SerialLink, find_lora_port
            assert SETTINGS.link.interface == 'hat'
            assert find_lora_port() == '/dev/serial0'
            assert SerialLink(115200).interface == 'hat'
            """))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_windows_com_port_is_used_verbatim(self):
        self.parser.set("link", "interface", "usb")
        self.parser.set("link", "port", "COM9")
        result = self.run_configured("from loraq_protocol import find_lora_port; assert find_lora_port() == 'COM9'")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_missing_link_section_defaults_to_auto(self):
        self.parser.remove_section("link")
        result = self.run_configured(
            "from loraq_config import SETTINGS; assert (SETTINGS.link.interface, SETTINGS.link.port) == ('auto', None)")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_legacy_program_on_connect_setting_does_not_write_on_open(self):
        self.parser.set("link", "program_on_connect", "true")
        result = self.run_configured(FAKE_SERIAL + textwrap.dedent("""
            from unittest.mock import patch
            import loraq_protocol as protocol
            with patch.object(protocol.serial, 'Serial', FakeSerial):
                link = protocol.SerialLink(115200, port='FAKE')
                assert link.ensure_open()
                assert bytes(link._ser.written) == b''
            """))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_removed_transmitter_config_mode_is_rejected(self):
        result = self.run_configured(textwrap.dedent("""
            import importlib.util, sys
            spec = importlib.util.spec_from_file_location('tx', 'LoRaQ-transmitter.py')
            tx = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = tx
            spec.loader.exec_module(tx)
            sys.argv = ['LoRaQ-transmitter.py', '--config']
            assert tx.main() == 2
            """))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("configuration is a separate operation", result.stdout)

    def test_serial_startup_error_and_recovery_in_graphics(self):
        result = self.run_configured(FAKE_SERIAL + textwrap.dedent("""
            from unittest.mock import patch
            from types import SimpleNamespace
            import pygame
            import loraq_protocol as protocol
            from loraq_gfx_engine import LoRaQEngine, COLOUR_MAP, VERSION

            pygame.font.init()
            connected_text = f'Station ID: CAB_1 [3]\\nVersion {VERSION}'
            unavailable_text = connected_text + '\\nLoRa device unavailable'
            for preview in (False, True):
                engine = LoRaQEngine(pygame.Surface((640, 510)), show_telemetry=preview,
                                     station_id='CAB_1', ack_slot=3)
                assert 'Station ID: CAB_1 [3]' in engine.state['area1_text']
                link = protocol.SerialLink(115200)
                with patch.object(protocol, 'find_lora_port', return_value=None), \\
                     patch.object(protocol, 'available_ports_hint', return_value='none enumerated'):
                    assert not link.ensure_open()
                assert engine.set_link_status(link.connected, link.last_error)
                assert engine.state['area1_text'] == unavailable_text
                assert 'Waiting for commands' not in engine.state['area1_text']
                assert engine.state['area1_colour'] == COLOUR_MAP['R']
                assert not engine.set_link_status(link.connected, link.last_error)
                engine.render()

                link.fixed_port = 'FAKE'
                link._next_attempt = 0
                with patch.object(protocol.serial, 'Serial', side_effect=OSError('port busy')):
                    assert not link.ensure_open()
                engine.set_link_status(link.connected, link.last_error)
                assert link.last_error == 'port busy'
                assert engine.state['area1_text'] == unavailable_text

                broken = FakeSerial()
                broken.reset_input_buffer = lambda: (_ for _ in ()).throw(OSError('initialisation failed'))
                link._next_attempt = 0
                with patch.object(protocol.serial, 'Serial', return_value=broken):
                    assert not link.ensure_open()
                assert not broken.is_open
                engine.set_link_status(link.connected, link.last_error)
                assert link.last_error == 'initialisation failed'
                assert engine.state['area1_text'] == unavailable_text

                link._next_attempt = 0
                with patch.object(protocol.serial, 'Serial', FakeSerial):
                    assert link.ensure_open()
                assert link.last_error is None
                engine.set_link_status(link.connected, link.last_error)
                assert engine.state['area1_text'] == connected_text
                assert 'Waiting for commands' not in engine.state['area1_text']
                assert engine.state['area1_colour'] == COLOUR_MAP['W']
                engine.render()

                link._drop(OSError('device unplugged'))
                engine.set_link_status(link.connected, link.last_error)
                assert link.last_error == 'device unplugged'
                assert engine.state['area1_text'] == unavailable_text
                engine.apply_records([SimpleNamespace(op='C', area='0')])
                assert not engine.set_link_status(True)
                assert engine.state['area1_text'] == ''
                engine.apply_records([SimpleNamespace(op='T', area='1', text='GO',
                                                     colour='W', timer=None, effect_id=0)])
                assert not engine.set_link_status(False, 'port busy')
                assert engine.state['area1_text'] == 'GO'
            pygame.quit()
            """))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_invalid_interface_is_rejected(self):
        self.parser.set("link", "interface", "spi")
        result = self.run_configured("import loraq_protocol")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("[link] interface", result.stderr)

    def test_receiver_runs_without_python_osc(self):
        result = self.run_configured(textwrap.dedent("""
            import importlib.util, sys

            class Blocker:
                def find_spec(self, name, path=None, target=None):
                    if name.split('.')[0] == 'pythonosc':
                        raise ImportError('pythonosc is not installed on receivers')
                    return None

            sys.meta_path.insert(0, Blocker())
            spec = importlib.util.spec_from_file_location('rx', 'LoRaQ-display.py')
            module = importlib.util.module_from_spec(spec)
            sys.modules['rx'] = module
            spec.loader.exec_module(module)
            """))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_ack_slot_scales_with_configured_air_rate(self):
        self.parser.set("radio", "spreading_factor", "12")
        self.parser.set("radio", "bandwidth_khz", "125")
        result = self.run_configured("import loraq_protocol as p; assert p.ACK_SLOT_S > p.lora_toa_s(len(p._max_ack)) + 0.02")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class AckTrackerTests(unittest.TestCase):
    def test_online_after_ack_is_visible_without_resetting_misses(self):
        spec = importlib.util.spec_from_file_location(
            "loraq_tx_test", Path(__file__).parent.parent / "LoRaQ-transmitter.py")
        transmitter = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = transmitter
        spec.loader.exec_module(transmitter)

        tracker = transmitter.AckTracker("BE50")
        tracker.on_online("STATION", "2.0", 0)
        tracker.open_window(14, "cue", time.monotonic() + 10)
        tracker.on_ack("STATION", "BE50", [14])
        tracker.on_online("STATION", "2.0", 0)
        _, _, state, label = tracker.snapshot()[-1]
        self.assertEqual(state, "OK")
        self.assertIn("ONLINE v2.0", label)
        self.assertIn("ACK #14", label)

        tracker.open_window(15, "cue", time.monotonic() - 1)
        tracker.poll()
        _, _, state, label = tracker.snapshot()[-1]
        self.assertEqual(state, "NO_RESPONSE")
        self.assertIn("ONLINE v2.0", label)
        self.assertIn("NO ACK", label)
        tracker.on_online("STATION", "2.0", 0)
        _, _, state, label = tracker.snapshot()[-1]
        self.assertEqual(state, "NO_RESPONSE")
        self.assertIn("NO ACK x1", label)

        tracker.open_window(16, "cue", time.monotonic() + 10)
        tracker.on_ack("STATION", "BE50", [16])
        _, _, state, label = tracker.snapshot()[-1]
        self.assertEqual((state, label), ("OK", "ACK #16"))


class PreviewLayoutTests(unittest.TestCase):
    def test_preview_statistics_do_not_cover_cue_on_resize(self):
        env = dict(os.environ, SDL_VIDEODRIVER="dummy")
        result = subprocess.run([sys.executable, "-c", textwrap.dedent("""
    import pygame
    from dataclasses import replace
    from loraq_gfx_engine import LoRaQEngine, STATS_BG
    from loraq_icon import STARTUP_BG
    import loraq_gfx_engine as gfx

    pygame.display.init()
    pygame.font.init()
    surface = pygame.display.set_mode((640, 510), pygame.RESIZABLE)
    engine = LoRaQEngine(surface, show_telemetry=True)
    engine.set_sent_seq(42)
    engine.set_stations([('CAB_1', 0, 'OK', 'ACK #42'), ('CAB_2', 1, 'MISSED', 'ACK MISSING x1 / ACK #41')])
    engine.update_duty_cycle(0.02, 0.7, 125.0)
    gfx.SETTINGS = replace(gfx.SETTINGS, transmitter=replace(gfx.SETTINGS.transmitter, max_dwell_time=100.0))
    engine.render()
    assert engine.screen.get_size() == (640, 360)
    assert engine.stage_rect.bottom == engine.stats_rect.top == 360
    assert surface.get_at((5, 5))[:3] == STARTUP_BG
    assert surface.get_at((5, 400))[:3] == STATS_BG
    duty_x = int(640 * 0.64) + 6
    duty_width = 640 - duty_x - 6
    assert (engine.font_status.size('Last TX dwell time:')[0]
            + engine.font_status.size('125.0 ms')[0] + 5 <= duty_width)
    assert surface.get_at((duty_x + 5, engine.stats_rect.top))[:3] == gfx.STATS_LINE
    duty_split = int(surface.get_width() * 0.64)
    duty_region = pygame.Rect(duty_split, engine.stats_rect.top,
                              surface.get_width() - duty_split, engine.stats_rect.height)
    assert any(surface.get_at((x, y))[:3] == (255, 90, 90)
               for x in range(duty_region.left, duty_region.right)
               for y in range(duty_region.top, duty_region.bottom))
    gfx.SETTINGS = replace(gfx.SETTINGS, transmitter=replace(gfx.SETTINGS.transmitter, max_dwell_time=0))
    engine.render()
    assert not any(surface.get_at((x, y))[:3] == (255, 90, 90)
                   for x in range(duty_region.left, duty_region.right)
                   for y in range(duty_region.top, duty_region.bottom))

    surface = pygame.display.set_mode((400, 320), pygame.RESIZABLE)
    engine.resize(surface)
    engine.render()
    assert engine.stage_rect.bottom == engine.stats_rect.top
    assert engine.stats_rect.bottom == 320
    assert abs(engine.width / engine.height - 16 / 9) < 0.02

    receiver = LoRaQEngine(pygame.Surface((640, 360)))
    receiver.render()
    assert receiver.screen.get_size() == (640, 360)
    pygame.quit()
    """)], env=env, text=True, capture_output=True, cwd=Path(__file__).parent.parent, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()