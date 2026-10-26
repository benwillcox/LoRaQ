import importlib.util
import os
import queue
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from loraq_protocol import (
    EFFECTS, Deduplicator, LineSplitter, ProtocolError, Record, crc16, decode_frame, encode_ack, encode_cue,
    encode_online, encode_osc, encode_reload, rssi_suffix,
)


def load_script(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).parent.parent / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class ParserEffectTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tx = load_script("effects_tx", "LoRaQ-transmitter.py")

    def test_display_syntax(self):
        for effect_id in EFFECTS:
            for args in (["GO", 1, "green", "00:30", effect_id],
                         [f"GO|1|green|30|{effect_id}"]):
                record = self.tx.build_item(args).records[0]
                self.assertEqual(record, Record("D", "1", "G", 30, effect_id, "GO"))

    def test_standalone_syntax(self):
        for command in ("alert", "/alert"):
            for effect_id in EFFECTS:
                self.assertEqual(self.tx.build_item([command, effect_id]).records,
                                 [Record("F", "0", effect_id=effect_id)])

    def test_oscsend_one_shot_command(self):
        item = self.tx.build_item(["oscsend"])
        self.assertEqual((item.kind, item.osc), ("osc", ""))
        with self.assertRaisesRegex(self.tx.CueError, "takes no parameters"):
            self.tx.build_item(["oscsend", "/workspace/go", 42])
        with self.assertRaisesRegex(self.tx.CueError, "oscsend"):
            self.tx.build_item(["osc", "/workspace/go", 42])

    def test_osc_standby_command_is_forwarded_on_empty_trigger(self):
        transmitter = self.tx.Transmitter(Mock())
        frame = SimpleNamespace(osc="", session="ABCD", seq=1)
        with patch.object(self.tx, "OSC_FORWARD_IP", "127.0.0.1"), \
                patch.object(self.tx, "OSC_FORWARD_PORT", 9000), \
                patch.object(self.tx.udp_client, "SimpleUDPClient") as client:
            transmitter.osc_standby.arm("test", "/workspace/go 42")
            transmitter.osc_standby.handle(frame)
        client.return_value.send_message.assert_called_once_with("/workspace/go", [42])
        self.assertFalse(transmitter.osc_standby.armed)

    def test_empty_osc_trigger_frame_decodes(self):
        frame = decode_frame(encode_osc("REMOTE", "ABCD", 1, 1, 1, ""))
        self.assertEqual((frame.kind, frame.osc), ("Q", ""))

    def test_invalid_options(self):
        for args in (["GO", "1", "green", "flash"], ["GO", "1", "green", "strobe"],
                     ["GO", "1", "green", "noflash"], ["GO", "1", "green", "30"],
                     ["GO", "1", "green", "8"], ["GO", "1", "green", "1", "2"],
                     ["GO", "1", "green", "00:30", "00:40"], ["GO|1|green|30|flash"],
                     ["alert"], ["alert", 1, 2], ["alert", -1], ["alert", 8],
                     ["alert", 1.5], ["alert", True], ["alert", "01"]):
            with self.subTest(args=args), self.assertRaises(self.tx.CueError):
                self.tx.build_item(args)

    def test_one_shot_forwarding(self):
        for args, address, payload in ((["/alert", "6"], "/alert", [6]),
                           (["/clear", "2"], "/clear", [2]),
                           (["clear"], "/clear", []),
                           (["oscsend"], "/oscsend", []),
                                       (["GO", "1", "green", "2"], "/display",
                                        ["GO", "1", "green", "2"])):
            with patch.object(self.tx, "osc_port_in_use", return_value=True), \
                    patch.object(self.tx.udp_client, "SimpleUDPClient") as client:
                self.assertEqual(self.tx.run_one_shot(args), 0)
                client.return_value.send_message.assert_called_once_with(address, payload)

    def test_effects_are_batching_barriers(self):
        transmitter = self.tx.Transmitter(Mock())
        cue = self.tx.build_item(["GO", "1", "green"])
        effect = self.tx.build_item(["alert", "2"])
        transmitter.queue.put(effect)
        batch, carry = transmitter._collect_batch(cue)
        self.assertEqual(batch, [cue])
        self.assertIs(carry, effect)
        transmitter.queue.put(cue)
        batch, carry = transmitter._collect_batch(effect)
        self.assertEqual(batch, [effect])
        self.assertIsNone(carry)
        self.assertIs(transmitter.queue.get_nowait(), cue)


class TransmitterRemoteEffectTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tx = load_script("remote_effects_tx", "LoRaQ-transmitter.py")

    def test_remote_bursts_play_once_and_new_sequence_retriggers(self):
        events = queue.Queue()
        transmitter = self.tx.Transmitter(Mock(), on_ui=events.put)
        transmitter.session = "FFFF"
        dedup = Deduplicator()
        record = Record("F", "0", effect_id=3)
        for burst in range(1, 4):
            transmitter._handle_line(encode_cue("REMOTE", "ABCD", 1, burst, 3, [record]), dedup)
        audio, engine = Mock(), Mock()
        self.tx.service_ui_events(engine, events, audio)
        audio.play_records.assert_called_once_with([record])
        engine.apply_records.assert_called_once_with([record])
        engine.set_remote_cue.assert_called_once_with("REMOTE", "ABCD", 1)
        transmitter._handle_line(encode_cue("REMOTE", "ABCD", 2, 1, 3, [record]), dedup)
        self.tx.service_ui_events(engine, events, audio)
        self.assertEqual(audio.play_records.call_count, 2)

    def test_received_rssi_is_logged(self):
        transmitter = self.tx.Transmitter(Mock())
        transmitter.session = "FFFF"
        line = encode_cue("REMOTE", "ABCD", 1, 1, 1, [Record("D", "1", text="GO")])
        with self.assertLogs("loraq", level="INFO") as messages:
            transmitter._handle_line(line, Deduplicator(), 0xD2)
        self.assertTrue(any(message.endswith("[RSSI: -46.0 dBm]")
                            and "Remote cue from another transmitter" in message
                            for message in messages.output))

    def test_local_cue_web_push_does_not_require_lora_dongle(self):
        link, web = Mock(), Mock()
        link.ensure_open.return_value = False
        events = []
        transmitter = self.tx.Transmitter(link, on_ui=events.append, web=web)
        transmitter.session = "ABCD"
        records = [Record("D", "1", text="LOCAL")]

        transmitter._transmit_batch([self.tx.TxItem("cue", records)])
        transmitter._transmit_batch([self.tx.TxItem("test", records)])

        web.submit.assert_called_once_with(transmitter.station_id, "ABCD", 1, records)
        self.assertNotIn(("web_push",), events)
        self.assertEqual(link.write.call_count, 0)
        ui_events = queue.Queue()
        ui_events.put(("web_status", 2, False, 1, False, "network down"))
        engine = Mock()
        self.tx.service_ui_events(engine, ui_events)
        engine.set_web_status.assert_called_once_with(2, False, 1, False, "network down")

    def test_web_queueing_starts_only_after_lora_bursts(self):
        web = Mock()
        link = Mock()
        transmitter = self.tx.Transmitter(link, web=web)
        records = [Record("D", "1", text="LOCAL")]

        def write(frame):
            web.submit.assert_not_called()
            return True

        link.write.side_effect = write
        with patch.object(self.tx, "DEFAULT_BURSTS", 3), \
                patch.object(self.tx, "burst_gap_s", return_value=0):
            transmitter._transmit_batch([self.tx.TxItem("cue", records)])

        self.assertEqual(link.write.call_count, 3)
        web.submit.assert_called_once_with(transmitter.station_id, transmitter.session, 1, records)

    def test_command_sources_reach_preview_once_including_osc_and_reload(self):
        events = queue.Queue()
        transmitter = self.tx.Transmitter(Mock(), on_ui=events.put)
        transmitter.session = "FFFF"
        dedup = Deduplicator()
        packets = (encode_cue("FIRST", "ABCD", 1, 1, 1, [Record("C", "0")]),
                   encode_reload("SECOND", "ABCD", 2, 1, 1),
                   encode_osc("THIRD", "ABCD", 3, 1, 1, ""))
        for index, data in enumerate(packets):
            transmitter._handle_line(data, dedup, 0xD2 if index == 0 else None)
            transmitter._handle_line(data, dedup, 0x80)
        transmitter._handle_line(encode_ack("ACKER", 0, "FFFF", [1]), dedup)
        transmitter._handle_line(encode_online("ONLINE", "1.0", 1), dedup)
        engine = Mock()
        self.tx.service_ui_events(engine, events)
        self.assertEqual([item.args for item in engine.set_received_source.call_args_list],
                         [("FIRST", 0xD2), ("SECOND", None), ("THIRD", None)])
        engine.clear_received_source.assert_not_called()
        events.put(("records", [Record("D", "1", text="LOCAL")]))
        events.put(("sent_seq", 1, True))
        self.tx.service_ui_events(engine, events)
        engine.clear_received_source.assert_not_called()
        events.put(("received_source", "HEADLESS", None))
        self.assertFalse(self.tx.service_ui_events(None, events))

    def test_all_transmission_kinds_carry_station_id(self):
        link = Mock()
        transmitter = self.tx.Transmitter(link)
        transmitter.station_id = "SOURCE"
        with patch.object(self.tx, "burst_gap_s", return_value=0):
            for kind in ("cue", "test", "reload", "osc"):
                records = [Record("D", "1", text="GO")] if kind in ("cue", "test") else []
                transmitter._transmit_batch([self.tx.TxItem(kind, records)])
        frames = [decode_frame(item.args[0]) for item in link.write.call_args_list]
        self.assertTrue(all(frame.station == "SOURCE" for frame in frames))
        self.assertEqual({frame.kind for frame in frames}, {"C", "R", "Q"})

    def test_oversized_frame_is_not_written(self):
        transmitter = self.tx.Transmitter(Mock())
        with self.assertRaises(ProtocolError):
            transmitter._transmit_batch([self.tx.TxItem("cue", [Record("D", "1", text="X" * 300)])])
        transmitter.link.write.assert_not_called()

    def test_ack_rssi_is_appended_to_ack_log(self):
        tracker = self.tx.AckTracker("ABCD")
        tracker._issued[1] = 1.0
        tracker._sent_seq = 1
        with self.assertLogs("loraq", level="INFO") as messages:
            tracker.on_ack("STATION", "ABCD", [1], 0xD2)
        self.assertTrue(any(message.endswith("[RSSI: -46.0 dBm]")
                            and "ACK from STATION for Seq 1" in message
                            for message in messages.output))

    def test_ack_rssi_is_in_transmission_stats_snapshot(self):
        tracker = self.tx.AckTracker("ABCD")
        tracker._issued[1] = 1.0
        tracker._sent_seq = 1
        tracker.on_ack("STATION", "ABCD", [1], 0xD2)
        station_id, slot, state, label = next(
            row for row in tracker.snapshot() if row[0] == "STATION")
        self.assertEqual(state, "OK")
        self.assertIn("ACK #1 [RSSI: -46.0 dBm]", label)

    def test_ack_slot_is_learned_and_extends_ack_window(self):
        tracker = self.tx.AckTracker("ABCD")
        tracker._issued[1] = 1.0
        tracker._sent_seq = 1
        frame = decode_frame(encode_ack("LATE", 9, "ABCD", [1]))
        tracker.on_ack(frame.station, frame.session, frame.acked, None, frame.slot)
        row = next(row for row in tracker.snapshot() if row[0] == "LATE")
        self.assertEqual(row[1], 9)
        self.assertEqual(tracker.slot_count(), 10)

    def test_own_echo_and_local_preview_are_silent(self):
        events = queue.Queue()
        transmitter = self.tx.Transmitter(Mock(), on_ui=events.put)
        records = [Record("D", "1", effect_id=2, text="GO")]
        transmitter._handle_line(encode_cue("REMOTE", transmitter.session, 1, 1, 3, records), Deduplicator())
        self.assertTrue(events.empty())
        events.put(("records", records))
        audio, engine = Mock(), Mock()
        self.tx.service_ui_events(engine, events, audio)
        engine.apply_records.assert_called_once_with(records)
        audio.play_records.assert_not_called()

    def test_headless_and_missing_audio_paths(self):
        for engine, audio in ((None, Mock()), (Mock(), None), (None, None)):
            records = [Record("F", "0", effect_id=7)]
            events = queue.Queue()
            events.put(("remote_records", records))
            events.put(("remote_cue", "REMOTE", "ABCD", 1))
            events.put(("stations", {}))
            self.tx.service_ui_events(engine, events, audio)
            self.assertTrue(events.empty())
            if audio is not None:
                audio.play_records.assert_called_once_with(records)

    def test_preview_redraws_only_for_changes_or_active_effects(self):
        import pygame
        for source in ("idle", "expose", "resize", "records", "remote_records", "stations",
                       "duty", "sent_seq", "tx_failed", "web_status", "remote_cue",
                       "osc_status", "link", "effect"):
            with self.subTest(source=source):
                transmitter, engine, audio = Mock(), Mock(), Mock()
                transmitter.stop_event.is_set.side_effect = [False] * 6 + [True]
                engine.set_link_status.side_effect = [False, False, source == "link", False, False, False]
                engine.has_active_effects.return_value = False
                if source == "effect":
                    engine.has_active_effects.side_effect = [False, True, False, False, False]
                events = queue.Queue()
                window_events = [[], [], [], [], [], []]
                if source == "expose":
                    window_events[2] = [pygame.event.Event(pygame.VIDEOEXPOSE)]
                elif source == "resize":
                    window_events[2] = [pygame.event.Event(pygame.VIDEORESIZE, w=800, h=600)]

                def enqueue_update(*args):
                    if source in ("records", "remote_records", "stations", "duty", "sent_seq",
                                  "tx_failed", "web_status", "remote_cue", "osc_status") and clock.tick.call_count == 2:
                        events.put((source, []))

                with patch.object(self.tx.pygame.time, "Clock") as clock_factory, \
                        patch.object(self.tx.pygame.event, "get", side_effect=window_events), \
                        patch.object(self.tx.pygame.display, "flip") as flip, \
                        patch.object(self.tx.pygame.display, "set_mode"):
                    clock = clock_factory.return_value
                    clock.tick.side_effect = enqueue_update
                    self.tx.run_preview_loop(transmitter, engine, events, audio)
                expected = 1 if source == "idle" else 2
                self.assertEqual(engine.render.call_count, expected)
                self.assertEqual(flip.call_count, expected)
                if source == "remote_records":
                    audio.play_records.assert_called_once_with([])

    def test_headless_startup_plays_remote_audio_and_registers_alert(self):
        records = [Record("F", "0", effect_id=3)]
        with patch.object(self.tx, "osc_port_in_use", return_value=False), \
                patch.object(self.tx, "GFX_AVAILABLE", False), \
                patch.object(self.tx, "SerialLink"), \
                patch.object(self.tx, "Transmitter") as transmitter_factory, \
                patch.object(self.tx, "RangeTest"), \
                patch.object(self.tx, "CueAudio") as audio_factory, \
                patch.object(self.tx.dispatcher, "Dispatcher") as dispatcher_factory, \
                patch.object(self.tx.osc_server, "ThreadingOSCUDPServer"), \
                patch.object(self.tx.threading, "Thread"), \
                patch.object(self.tx, "print_banner"), \
                patch.object(self.tx, "print_help"):
            transmitter = transmitter_factory.return_value
            transmitter.stop_event.wait.side_effect = [False, True]
            transmitter.start.side_effect = lambda: transmitter_factory.call_args.kwargs["on_ui"](
                ("remote_records", records))
            self.assertEqual(self.tx.run_interactive(), 0)
            self.assertFalse(transmitter_factory.call_args.kwargs["preview_enabled"])
            audio_factory.assert_called_once_with(self.tx.SETTINGS.transmitter.audio_enabled)
            audio_factory.return_value.play_records.assert_called_once_with(records)
            audio_factory.return_value.close.assert_called_once()
            mappings = {call.args[0]: call.args[1]
                        for call in dispatcher_factory.return_value.map.call_args_list}
            self.assertIn("/alert", mappings)
            self.assertIn("/clear", mappings)
            self.assertNotIn("/flash", mappings)
            mappings["/clear"]("/clear", 2)
            self.assertEqual(transmitter.submit.call_args.args[0].records,
                             [Record("C", "2")])
            mappings["/alert"]("/alert", 7)
            self.assertEqual(transmitter.submit.call_args.args[0].records,
                             [Record("F", "0", effect_id=7)])


class TransmitterPreviewAckTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tx = load_script("preview_ack_tx", "LoRaQ-transmitter.py")

    def setUp(self):
        self.link = Mock(read_timeout=0.05)
        self.events = queue.Queue()
        self.transmitter = self.tx.Transmitter(self.link, self.events.put, preview_enabled=True)
        self.transmitter.session = "FFFF"
        self.dedup = Deduplicator()

    def test_slotted_ack_uses_receiver_identity_and_closes_sender_window(self):
        line = encode_cue("REMOTE", "ABCD", 1, 1, 1, [Record("D", "1", text="GO")])
        with patch.object(self.tx.time, "monotonic", return_value=10.0):
            self.transmitter._handle_line(line, self.dedup)
            due = self.transmitter._pending_acks["ABCD"]
            self.transmitter._service_preview_acks()
        self.link.write.assert_not_called()
        with patch.object(self.tx.time, "monotonic", return_value=due):
            self.transmitter._service_preview_acks()
        frame = decode_frame(self.link.write.call_args.args[0])
        self.assertEqual((frame.kind, frame.station, frame.slot, frame.session, frame.acked),
                         ("A", self.tx.SETTINGS.station.station_id, self.tx.SETTINGS.station.ack_slot,
                          "ABCD", [1]))
        with patch.object(self.tx, "EXPECTED_STATIONS", {frame.station: frame.slot}):
            tracker = self.tx.AckTracker("ABCD")
        tracker.open_window(1, "GO", due + 1)
        tracker.on_ack(frame.station, frame.session, frame.acked, slot=frame.slot)
        self.assertIn(frame.station, tracker._open[1].acked)

    def test_duplicate_bursts_extend_deadline_without_reapplying(self):
        with patch.object(self.tx, "ack_delay_s", return_value=1.0), \
                patch.object(self.tx.time, "monotonic", return_value=10.0):
            self.transmitter._handle_line(encode_cue("REMOTE", "ABCD", 1, 1, 3, [Record("F", "0", effect_id=3)]), self.dedup)
        with patch.object(self.tx, "ack_delay_s", return_value=0.9), \
                patch.object(self.tx.time, "monotonic", return_value=10.2):
            self.transmitter._handle_line(encode_cue("REMOTE", "ABCD", 1, 2, 3, [Record("F", "0", effect_id=3)]), self.dedup)
        self.assertAlmostEqual(self.transmitter._pending_acks["ABCD"], 11.1)
        self.assertEqual(len(self.transmitter._recent_remote), 1)
        self.assertEqual(self.events.qsize(), 3)

    def test_reload_and_unarmed_osc_are_receipt_acked_once(self):
        for seq, line in enumerate((encode_reload("REMOTE", "ABCD", 1, 1, 1),
                                    encode_osc("REMOTE", "ABCD", 2, 1, 1, "")), 1):
            with patch.object(self.tx.time, "monotonic", return_value=float(seq)):
                self.transmitter._handle_line(line, self.dedup)
                self.transmitter._handle_line(line, self.dedup)
        with patch.object(self.tx.time, "monotonic", return_value=3.0):
            self.transmitter._service_preview_acks()
        self.assertEqual(decode_frame(self.link.write.call_args.args[0]).acked, [1, 2])
        self.assertFalse(self.transmitter.stop_event.is_set())
        self.assertFalse(self.transmitter.osc_standby.armed)

    def test_no_acks_for_own_echo_ack_online_invalid_or_headless(self):
        for line in (encode_cue("REMOTE", "FFFF", 1, 1, 1, [Record("F", "0", effect_id=3)]),
                     encode_ack("REMOTE", 0, "FFFF", [1]), encode_online("REMOTE", "2.1", 0), b"~invalid"):
            self.transmitter._handle_line(line, self.dedup)
        self.assertFalse(self.transmitter._pending_acks)
        headless = self.tx.Transmitter(self.link, on_ui=self.events.put)
        headless.session = "FFFF"
        headless._handle_line(encode_reload("REMOTE", "ABCD", 1, 1, 1), Deduplicator())
        self.assertFalse(headless._pending_acks)
        self.link.write.assert_not_called()

    def test_timeout_stop_and_expired_history(self):
        self.transmitter._pending_acks["ABCD"] = 10.01
        with patch.object(self.tx.time, "monotonic", return_value=10.0):
            self.assertAlmostEqual(self.transmitter._preview_read_timeout(), 0.01)
        self.transmitter._recent_remote.append(("ABCD", 1, 0.0))
        with patch.object(self.tx.time, "monotonic", return_value=self.tx.ACK_HISTORY_S + 20):
            self.transmitter._service_preview_acks()
        self.link.write.assert_not_called()
        self.transmitter._pending_acks["ABCD"] = 20
        self.transmitter.stop()
        self.assertFalse(self.transmitter._pending_acks)

    def test_station_identity_and_local_expected_station_exclusion(self):
        station = SimpleNamespace(station_id="LOCAL", ack_slot=7)
        with patch.object(self.tx, "SETTINGS", SimpleNamespace(station=station)), \
                patch.object(self.tx.socket, "gethostname", side_effect=AssertionError("hostname fallback")), \
                patch.object(self.tx, "EXPECTED_STATIONS", {"LOCAL": 7}):
            transmitter = self.tx.Transmitter(self.link, preview_enabled=True)
        self.assertEqual(transmitter.station_id, "LOCAL")
        self.assertEqual(transmitter.ack_slot, 7)
        self.assertFalse(transmitter.tracker.snapshot())

    def test_cumulative_limit_and_source_sessions_remain_separate(self):
        with patch.object(self.tx.time, "monotonic", return_value=10.0):
            for seq in range(1, 7):
                self.transmitter._handle_line(encode_reload("REMOTE", "ABCD", seq, 1, 1), self.dedup)
            self.transmitter._handle_line(encode_reload("REMOTE", "1234", 1, 1, 1), self.dedup)
        with patch.object(self.tx.time, "monotonic", return_value=11.0):
            self.transmitter._service_preview_acks()
        first = decode_frame(self.link.write.call_args.args[0])
        self.assertEqual((first.session, first.acked), ("ABCD", [3, 4, 5, 6]))
        with patch.object(self.tx.time, "monotonic", return_value=12.0):
            self.transmitter._service_preview_acks()
        second = decode_frame(self.link.write.call_args.args[0])
        self.assertEqual((second.session, second.acked), ("1234", [1]))

    def test_ack_waits_for_local_burst_and_counts_airtime_without_sent_event(self):
        with patch.object(self.tx.time, "monotonic", return_value=10.0):
            self.transmitter._handle_line(encode_reload("REMOTE", "ABCD", 1, 1, 1), self.dedup)
        self.transmitter._transmitting = True
        with patch.object(self.tx.time, "monotonic", return_value=11.0):
            self.transmitter._service_preview_acks()
        self.link.write.assert_not_called()
        self.transmitter._transmitting = False
        self.transmitter._radio_busy_until = 11.5
        with patch.object(self.tx.time, "monotonic", return_value=11.0):
            self.transmitter._service_preview_acks()
            self.assertEqual(self.transmitter._preview_read_timeout(), 0.05)
        self.link.write.assert_not_called()
        with patch.object(self.tx.time, "monotonic", return_value=11.5):
            self.transmitter._service_preview_acks()
        self.assertGreater(self.transmitter._radio_busy_until, 11.5)
        self.assertEqual(len(self.transmitter._duty._events), 1)
        events = list(self.events.queue)
        self.assertIn("duty", [event[0] for event in events])
        self.assertNotIn("sent_seq", [event[0] for event in events])
        self.assertFalse(self.transmitter.tracker._open)

    def test_queued_transmission_waits_for_pending_ack_and_reserved_channel(self):
        clock = [10.0]
        with patch.object(self.tx.time, "monotonic", side_effect=lambda: clock[0]):
            self.transmitter._handle_line(encode_reload("REMOTE", "ABCD", 1, 1, 1), self.dedup)
            reserved = self.transmitter._remote_free_at

            def advance(timeout):
                if self.transmitter._pending_acks:
                    clock[0] = self.transmitter._pending_acks["ABCD"]
                    self.transmitter._service_preview_acks()
                else:
                    clock[0] = max(reserved, self.transmitter._radio_busy_until)

            with patch.object(self.transmitter._radio_changed, "wait", side_effect=advance) as wait:
                self.assertTrue(self.transmitter._begin_transmission())
        self.assertTrue(wait.called)
        self.assertGreaterEqual(clock[0], reserved)
        self.link.write.assert_called_once()
        self.assertTrue(self.transmitter._transmitting)

    def test_write_failure_clears_pending_and_stop_interrupts_channel_wait(self):
        self.link.write.return_value = False
        with patch.object(self.tx.time, "monotonic", return_value=10.0):
            self.transmitter._handle_line(encode_reload("REMOTE", "ABCD", 1, 1, 1), self.dedup)
        with patch.object(self.tx.time, "monotonic", return_value=11.0):
            self.transmitter._service_preview_acks()
        self.assertFalse(self.transmitter._pending_acks)
        self.assertFalse(self.transmitter._duty._events)
        self.transmitter._channel_free_at = float("inf")
        with patch.object(self.transmitter._radio_changed, "wait", side_effect=lambda timeout: self.transmitter.stop()):
            self.assertFalse(self.transmitter._begin_transmission())
        self.assertFalse(self.transmitter._transmitting)

    def test_local_bursts_do_not_interleave_with_preview_ack(self):
        written = []
        clock = [10.0]

        def write(data):
            frame = decode_frame(data)
            written.append(frame.kind)
            if frame.kind == "C":
                self.transmitter._handle_line(encode_reload("REMOTE", "ABCD", 1, 1, 1), self.dedup)
                clock[0] += 1
                self.transmitter._service_preview_acks()
            return True

        self.link.write.side_effect = write
        with patch.object(self.tx.time, "monotonic", side_effect=lambda: clock[0]), \
                patch.object(self.transmitter.stop_event, "wait", return_value=False), \
                patch.object(self.tx, "DEFAULT_BURSTS", 3):
            self.transmitter._transmit([self.tx.TxItem("cue", [Record("D", "1", text="LOCAL")])])
            self.assertEqual(written, ["C", "C", "C"])
            clock[0] = self.transmitter._radio_busy_until
            self.transmitter._service_preview_acks()
        self.assertEqual(written, ["C", "C", "C", "A"])
        self.assertFalse(self.transmitter._transmitting)

    def test_graphics_initialization_controls_preview_acks(self):
        for available in (True, False):
            with self.subTest(graphics_initialized=available), \
                    patch.object(self.tx, "osc_port_in_use", return_value=False), \
                    patch.object(self.tx, "GFX_AVAILABLE", True), \
                    patch.object(self.tx.pygame.display, "init"), \
                    patch.object(self.tx.pygame.font, "init"), \
                    patch.object(self.tx.pygame.display, "set_caption"), \
                    patch.object(self.tx.pygame.display, "set_mode",
                                 side_effect=None if available else self.tx.pygame.error("no display")), \
                    patch.object(self.tx.pygame, "quit"), \
                    patch.object(self.tx, "LoRaQEngine"), \
                    patch.object(self.tx, "SerialLink"), \
                    patch.object(self.tx, "Transmitter") as factory, \
                    patch.object(self.tx, "RangeTest"), \
                    patch.object(self.tx, "CueAudio"), \
                    patch.object(self.tx.osc_server, "ThreadingOSCUDPServer"), \
                    patch.object(self.tx.threading, "Thread"), \
                    patch.object(self.tx, "print_banner"), \
                    patch.object(self.tx, "print_help"), \
                    patch.object(self.tx, "run_preview_loop"):
                factory.return_value.stop_event.wait.return_value = True
                self.assertEqual(self.tx.run_interactive(), 0)
                self.assertEqual(factory.call_args.kwargs["preview_enabled"], available)

    def test_one_shot_does_not_enable_preview_acks(self):
        with patch.object(self.tx, "osc_port_in_use", return_value=False), \
                patch.object(self.tx, "SerialLink") as link_factory, \
                patch.object(self.tx, "Transmitter") as factory, \
                patch.object(self.tx, "create_pusher", return_value=Mock()) as create_pusher:
            factory.return_value._channel_free_at = 0.0
            factory.return_value.failures = 0
            self.assertEqual(self.tx.run_one_shot(["alert", "3"]), 0)
            factory.assert_called_once_with(link_factory.return_value, web=create_pusher.return_value)
            self.assertFalse(factory.call_args.kwargs.get("preview_enabled", False))


class RendererEffectTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
        import pygame
        pygame.font.init()

    def test_timers_redraw_only_when_digits_change(self):
        import pygame
        from loraq_gfx_engine import LoRaQEngine
        for area in (1, 2):
            with self.subTest(area=area):
                engine = LoRaQEngine(pygame.Surface((640, 360)))
                with patch("loraq_gfx_engine.time.monotonic", return_value=100.0):
                    engine.apply_records([Record("D", str(area), "G", 3, text="GO")])
                    engine.anim_state = 'VISIBLE' if area == 2 else 'HIDDEN'
                    self.assertTrue(engine.has_active_effects(100.0))
                    engine.render()
                for now in (100.01, 100.5, 100.99):
                    self.assertFalse(engine.has_active_effects(now))
                for now, remaining in ((101.0, 2), (102.4, 1), (105.0, 0)):
                    self.assertTrue(engine.has_active_effects(now))
                    with patch("loraq_gfx_engine.time.monotonic", return_value=now):
                        engine.render()
                    key = engine._timer1_key if area == 1 else engine._timer2_key
                    self.assertEqual(key[0], remaining)
                    self.assertFalse(engine.has_active_effects(now + 0.01))
                self.assertFalse(engine.has_active_effects(200.0))
                self.assertEqual(engine.state[f'area{area}_start_time'], 100.0)

    def test_dual_timers_skip_unchanged_frames_and_restart(self):
        import pygame
        from loraq_gfx_engine import LoRaQEngine
        engine = LoRaQEngine(pygame.Surface((640, 360)))
        with patch("loraq_gfx_engine.time.monotonic", return_value=100.0):
            engine.apply_records([Record("D", "1", timer=10, text="GO"),
                                  Record("D", "2", timer=20, text="STANDBY")])
            engine.anim_state = 'VISIBLE'
            engine.render()
        redraws = 0
        for frame in range(1, 301):
            now = 100.0 + frame / 30
            if engine.has_active_effects(now):
                with patch("loraq_gfx_engine.time.monotonic", return_value=now):
                    engine.render()
                redraws += 1
        self.assertEqual(redraws, 10)
        self.assertEqual(engine._timer1_key[0], 0)
        self.assertEqual(engine._timer2_key[0], 10)
        with patch("loraq_gfx_engine.time.monotonic", return_value=110.5):
            engine.apply_records([Record("D", "2", timer=5, text="NEXT")])
            self.assertTrue(engine.has_active_effects(110.5))
            engine.render()
        self.assertFalse(engine.has_active_effects(111.0))
        self.assertTrue(engine.has_active_effects(111.5))
        with patch("loraq_gfx_engine.time.monotonic", return_value=111.5):
            engine.render()
        self.assertEqual(engine._timer2_key[0], 4)
        with patch("loraq_gfx_engine.time.monotonic", return_value=112.0):
            engine.resize(pygame.Surface((800, 480)))
            self.assertTrue(engine.has_active_effects(112.0))
            engine.render()
        self.assertFalse(engine.has_active_effects(112.1))

    def test_hidden_zone2_timer_does_not_request_redraws(self):
        import pygame
        from loraq_gfx_engine import LoRaQEngine
        engine = LoRaQEngine(pygame.Surface((640, 360)))
        engine.apply_records([Record("D", "2", timer=30, text="")])
        self.assertFalse(engine.has_active_effects(100.0))

    def test_flashes_redraw_only_on_colour_changes(self):
        import pygame
        from loraq_gfx_engine import LoRaQEngine
        engine = LoRaQEngine(pygame.Surface((640, 360)))
        with patch("loraq_gfx_engine.time.monotonic", return_value=100.0):
            engine.apply_records([Record("F", "0", effect_id=1)])
            self.assertTrue(engine.has_active_effects(100.0))
            engine.render()
        self.assertFalse(engine.has_active_effects(100.1))
        self.assertTrue(engine.has_active_effects(100.2))
        with patch("loraq_gfx_engine.time.monotonic", return_value=100.2):
            engine.render()
        self.assertFalse(engine.has_active_effects(100.3))
        self.assertTrue(engine.has_active_effects(100.4))
        with patch("loraq_gfx_engine.time.monotonic", return_value=100.4):
            engine.render()
        self.assertTrue(engine.has_active_effects(102.0))
        with patch("loraq_gfx_engine.time.monotonic", return_value=102.0):
            engine.render()
        self.assertFalse(engine.has_active_effects(102.1))

    def test_slides_still_request_intermediate_frames(self):
        import pygame
        from loraq_gfx_engine import LoRaQEngine
        engine = LoRaQEngine(pygame.Surface((640, 360)))
        with patch("loraq_gfx_engine.time.monotonic", return_value=100.0):
            engine.apply_records([Record("D", "2", text="GO")])
            engine.render()
        self.assertTrue(engine.has_active_effects(100.1))
        with patch("loraq_gfx_engine.time.monotonic", return_value=100.1):
            engine.render()
        self.assertLess(engine.current_panel_y, engine.height)
        self.assertGreater(engine.current_panel_y, engine.area2_top_bound)
        with patch("loraq_gfx_engine.time.monotonic", return_value=100.4):
            engine.render()
        self.assertFalse(engine.has_active_effects(100.5))
        with patch("loraq_gfx_engine.time.monotonic", return_value=101.0):
            engine.apply_records([Record("C", "2")])
            engine.render()
        self.assertTrue(engine.has_active_effects(101.1))
        with patch("loraq_gfx_engine.time.monotonic", return_value=101.4):
            engine.render()
        self.assertFalse(engine.has_active_effects(101.5))

    def test_tx_highlight_redraws_at_expiry_only(self):
        import pygame
        from loraq_gfx_engine import LoRaQEngine
        engine = LoRaQEngine(pygame.Surface((640, 510)), show_telemetry=True)
        with patch("loraq_gfx_engine.time.monotonic", return_value=100.0):
            engine.set_sent_seq(1)
            engine.render()
        self.assertFalse(engine.has_active_effects(100.1))
        self.assertTrue(engine.has_active_effects(100.5))
        with patch("loraq_gfx_engine.time.monotonic", return_value=100.5):
            engine.render()
        self.assertFalse(engine.has_active_effects(100.6))

    def test_web_status_distinguishes_pending_failure_and_success(self):
        import pygame
        from loraq_gfx_engine import LoRaQEngine
        engine = LoRaQEngine(pygame.Surface((640, 510)), show_telemetry=True)
        font = engine.font_status_heading
        rendered = {}

        class CapturingFont:
            def __getattr__(self, name):
                return getattr(font, name)

            def render(self, text, *args, **kwargs):
                rendered[text] = args[1]
                return font.render(text, *args, **kwargs)

        engine.font_status_heading = CapturingFont()
        with patch("loraq_gfx_engine.time.monotonic", return_value=100.0):
            engine.sent_seq = 7
            engine.render()
            self.assertEqual(rendered["LORA #7"], rendered["WEB"])
            self.assertEqual(rendered["LORA #7"], (255, 255, 255))
            engine.set_web_status(1, None, 1)
            with patch("loraq_gfx_engine.pygame.draw.rect", wraps=pygame.draw.rect) as draw_rect:
                engine.render()
        self.assertFalse(engine._web_highlighted)
        pending_colours = [call.args[1] for call in draw_rect.call_args_list]
        self.assertIn((255, 165, 0), pending_colours)
        with patch("loraq_gfx_engine.time.monotonic", return_value=100.1):
            engine.set_web_status(2, False, 1, error="network down")
            with patch("loraq_gfx_engine.pygame.draw.rect", wraps=pygame.draw.rect) as draw_rect:
                engine.render()
        self.assertTrue(engine.web_failed)
        failed_colours = [call.args[1] for call in draw_rect.call_args_list]
        self.assertIn((255, 50, 50), failed_colours)
        self.assertNotIn((255, 230, 0), failed_colours)
        with patch("loraq_gfx_engine.time.monotonic", return_value=100.2):
            engine.set_web_status(3, True, 0, delivered=True)
            with patch("loraq_gfx_engine.pygame.draw.rect", wraps=pygame.draw.rect) as draw_rect:
                engine.render()
        self.assertFalse(engine.web_failed)
        self.assertTrue(engine._web_highlighted)
        self.assertIn((255, 230, 0), [call.args[1] for call in draw_rect.call_args_list])
        engine.set_web_status(2, False, 1, error="stale network failure")
        self.assertFalse(engine.web_failed)
        self.assertEqual(engine.web_pending, 0)
        self.assertTrue(engine.has_active_effects(100.7))
        with patch("loraq_gfx_engine.time.monotonic", return_value=100.7):
            engine.render()
        self.assertFalse(engine.has_active_effects(100.8))

    def test_web_indicator_is_hidden_when_web_push_is_disabled(self):
        import pygame
        from loraq_gfx_engine import LoRaQEngine
        engine = LoRaQEngine(pygame.Surface((640, 510)), show_telemetry=True, show_web_info=False)
        font = engine.font_status_heading
        rendered = []

        class CapturingFont:
            def __getattr__(self, name):
                return getattr(font, name)

            def render(self, text, *args, **kwargs):
                rendered.append(text)
                return font.render(text, *args, **kwargs)

        engine.font_status_heading = CapturingFont()
        engine.set_web_status(1, False, 1, error="network down")
        engine.render()
        self.assertNotIn("WEB", rendered)
        self.assertIn("LORA --", rendered)

    def test_timer_borders_stay_fixed(self):
        import pygame
        from loraq_gfx_engine import COLOUR_MAP, LoRaQEngine
        for size in ((640, 360), (1280, 720)):
            for area in (1, 2):
                with self.subTest(size=size, area=area):
                    engine = LoRaQEngine(pygame.Surface(size))
                    engine.apply_records([Record("D", str(area), "G", 5999, text="GO")])
                    engine.state[f'area{area}_start_time'] = 0.0
                    engine.current_panel_y = engine.area2_top_bound
                    borders = []
                    for remaining in (5999, 11 * 60 + 11, 8 * 60 + 8, 1, 0):
                        with patch("loraq_gfx_engine.pygame.draw.rect", wraps=pygame.draw.rect) as draw:
                            if area == 1:
                                engine._draw_area1(5999 - remaining, COLOUR_MAP['G'])
                                timer_surface = engine._timer1_surface
                            else:
                                engine._draw_area2(5999 - remaining, COLOUR_MAP['G'], (0, 0, 0))
                                timer_surface = engine._timer2_surface
                        border = pygame.Rect(draw.call_args.args[2])
                        borders.append(border)
                        self.assertGreaterEqual(border.width, timer_surface.get_width() + 20)
                    self.assertTrue(all(border == borders[0] for border in borders))

    def test_standalone_effects_preserve_display(self):
        import pygame
        from loraq_gfx_engine import COLOUR_MAP, LoRaQEngine
        for effect_id in EFFECTS:
            engine = LoRaQEngine(pygame.Surface((640, 360)))
            engine.apply_records([Record("D", "1", "G", 30, text="GO"),
                                  Record("D", "2", "R", 60, text="STANDBY")])
            engine.state['area1_start_time'] = 123.0
            engine.state['area2_start_time'] = 124.0
            before = dict(engine.state)
            animation = engine.anim_state
            engine.apply_records([Record("F", "0", effect_id=effect_id)])
            for key, value in before.items():
                if not key.startswith("flash_"):
                    self.assertEqual(engine.state[key], value, key)
            self.assertEqual(engine.anim_state, animation)
            self.assertFalse(engine._startup_active)
            self.assertEqual(engine.state['flash_end_time'] > 0, EFFECTS[effect_id][0])
            if EFFECTS[effect_id][0]:
                self.assertEqual(engine.state['flash_colour'], COLOUR_MAP['G'])
                self.assertAlmostEqual(engine.state['flash_end_time'] - engine.state['flash_start_time'], 2)

    def test_sound_only_display_does_not_flash(self):
        import pygame
        from loraq_gfx_engine import LoRaQEngine
        engine = LoRaQEngine(pygame.Surface((640, 360)))
        engine.apply_records([Record("D", "1", "G", 30, 3, "GO")])
        self.assertEqual(engine.state['area1_text'], "GO")
        self.assertEqual(engine.state['flash_end_time'], 0)


class AudioEffectTests(unittest.TestCase):
    def test_pcm_patterns(self):
        from loraq_audio import SAMPLE_RATE, sound_samples
        for name, duration in (("buzzer", 0.6), ("beep", 0.2), ("double-beep", 0.4)):
            samples = sound_samples(name)
            self.assertEqual(len(samples), int(SAMPLE_RATE * duration))
            self.assertGreater(max(samples), 0)
            self.assertLess(min(samples), 0)
            self.assertLessEqual(max(abs(sample) for sample in samples), 32767)
            self.assertGreater(max(abs(sample) for sample in samples), 32000)
            self.assertEqual((samples[0], samples[-1]), (0, 0))
        samples = sound_samples("double-beep")
        self.assertTrue(all(sample == 0 for sample in samples[int(SAMPLE_RATE * 0.15):
                                                              int(SAMPLE_RATE * 0.25)]))

    def test_disabled_audio_does_not_initialize_mixer(self):
        from loraq_audio import CueAudio
        with patch("loraq_audio.pygame.mixer.init") as initialize, \
                self.assertLogs("loraq.audio", level="WARNING") as messages:
            audio = CueAudio(False)
            audio.play_records([Record("F", "0", effect_id=2)])
            audio.close()
            initialize.assert_not_called()
        self.assertIn("Cue audio disabled by configuration", messages.output[0])
        self.assertIn("audio_enabled = true", messages.output[0])

    def test_mapping_and_one_sound_per_frame(self):
        from loraq_audio import CueAudio
        audio = CueAudio(False)
        audio.channel = Mock()
        audio.sounds = {name: object() for name in ("buzzer", "beep", "double-beep")}
        for effect_id, (_, sound, _) in EFFECTS.items():
            audio.channel.reset_mock()
            audio.play_records([Record("F", "0", effect_id=effect_id)])
            if sound:
                audio.channel.play.assert_called_once_with(audio.sounds[sound])
            else:
                audio.channel.play.assert_not_called()
        audio.channel.reset_mock()
        audio.play_records([Record("D", "1", effect_id=2), Record("D", "2", effect_id=7)])
        audio.channel.play.assert_called_once_with(audio.sounds["double-beep"])

    def test_init_and_play_failure_disable_only_audio(self):
        import pygame
        from loraq_audio import CueAudio
        with patch("loraq_audio.pygame.mixer.init", side_effect=pygame.error("no audio device")), \
                self.assertLogs("loraq.audio", level="WARNING"):
            audio = CueAudio(True)
        self.assertIsNone(audio.channel)
        audio.channel = Mock()
        audio.sounds = {"buzzer": object()}
        audio.channel.play.side_effect = pygame.error("device lost")
        with self.assertLogs("loraq.audio", level="WARNING"):
            audio.play_records([Record("F", "0", effect_id=3)])
        self.assertIsNone(audio.channel)

    def test_real_dummy_mixer_lifecycle(self):
        import pygame
        from loraq_audio import CueAudio, SAMPLE_RATE
        pygame.mixer.quit()
        with patch.dict(os.environ, SDL_AUDIODRIVER="dummy"), \
                patch("loraq_audio.pygame.mixer.init", wraps=pygame.mixer.init) as initialize:
            audio = CueAudio(True)
            initialize.assert_called_once_with(frequency=SAMPLE_RATE, size=-16, channels=1,
                                               buffer=2048, allowedchanges=0)
            self.assertIsNotNone(audio.channel)
            self.assertEqual(audio.channel.get_volume(), 1.0)
            audio.play_records([Record("F", "0", effect_id=2)])
            self.assertTrue(audio.channel.get_busy())
            audio.play_records([Record("F", "0", effect_id=7)])
            self.assertIs(audio.channel.get_sound(), audio.sounds["double-beep"])
            audio.close()
            self.assertIsNone(pygame.mixer.get_init())


class ReceiverEffectTests(unittest.TestCase):
    def test_radio_loop_waits_until_deadlines_and_stops(self):
        receiver = load_script("wait_loop_rx", "LoRaQ-display.py")
        for kind in ("idle", "ack", "online", "reload"):
            with self.subTest(kind=kind):
                link = Mock(connected=True, read_timeout=0.25)
                reload_event = threading.Event()
                radio = receiver.RadioThread(link, "TEST", 1, queue.Queue(), reload_event)
                now = [100.0]
                waits = []
                if kind in ("ack", "reload"):
                    radio._recent.append(("ABCD", 1, 100.0))
                    radio._pending_ack = receiver.PendingAck("ABCD", 100.04)
                    radio._reload_after_ack = kind == "reload"
                elif kind == "online":
                    radio._online_due = 100.02

                def read_available(timeout):
                    waits.append(timeout)
                    now[0] += timeout
                    if len(waits) == (4 if kind == "idle" else 1):
                        radio.stop_event.set()
                    return b""

                link.read_available.side_effect = read_available
                with patch.object(receiver.time, "monotonic", side_effect=lambda: now[0]):
                    radio.run()
                if kind == "idle":
                    self.assertEqual(waits, [0.25] * 4)
                    self.assertEqual(now[0], 101.0)
                    link.write.assert_not_called()
                else:
                    self.assertAlmostEqual(waits[0], 0.02 if kind == "online" else 0.04)
                    frame = decode_frame(link.write.call_args.args[0])
                    self.assertEqual(frame.kind, "O" if kind == "online" else "A")
                    self.assertIsNone(radio._pending_ack)
                    self.assertIsNone(radio._online_due)
                    self.assertEqual(reload_event.is_set(), kind == "reload")

    def test_radio_loop_reconnects_and_applies_incoming_cue(self):
        receiver = load_script("wait_reconnect_rx", "LoRaQ-display.py")
        link = Mock(connected=False, read_timeout=0.25)
        frames = queue.Queue()
        radio = receiver.RadioThread(link, "TEST", 1, frames, threading.Event())

        def reconnect():
            link.connected = True
            return True

        def read_available(timeout):
            self.assertEqual(timeout, 0.25)
            radio.stop_event.set()
            return encode_cue("REMOTE", "ABCD", 1, 1, 1, [Record("D", "2", text="GO")]) + b"\xD2"

        link.ensure_open.side_effect = reconnect
        link.read_available.side_effect = read_available
        with patch.object(receiver.time, "monotonic", return_value=100.0), \
                patch.object(receiver.random, "uniform", return_value=1.0):
            radio.run()
        link.ensure_open.assert_called_once()
        self.assertEqual(frames.get_nowait().records[0].text, "GO")
        self.assertIsNotNone(radio._pending_ack)
        self.assertEqual(radio._online_due, 101.0)

    def test_radio_read_wait_is_capped_by_nearest_deadline(self):
        receiver = load_script("deadline_rx", "LoRaQ-display.py")
        radio = receiver.RadioThread(SimpleNamespace(read_timeout=0.25), "TEST", 1,
                                     queue.Queue(), Mock())
        with patch.object(receiver.time, "monotonic", return_value=100.0):
            self.assertEqual(radio._read_timeout(), 0.25)
            radio._online_due = 101.0
            self.assertEqual(radio._read_timeout(), 0.25)
            radio._pending_ack = receiver.PendingAck("ABCD", 100.04)
            self.assertAlmostEqual(radio._read_timeout(), 0.04)
            radio._online_due = 100.02
            self.assertAlmostEqual(radio._read_timeout(), 0.02)
            radio._online_due = 99.0
            self.assertEqual(radio._read_timeout(), 0.0)
            radio._online_due = None
            radio._pending_ack.due = 100.0
            self.assertEqual(radio._read_timeout(), 0.0)

    def test_radio_bursts_apply_once_and_retrigger_with_new_sequence(self):
        receiver = load_script("effects_rx", "LoRaQ-display.py")
        frames = queue.Queue()
        radio = receiver.RadioThread(Mock(), "TEST", 1, frames, Mock())
        record = Record("F", "0", effect_id=2)
        for burst in range(1, 4):
            radio._handle_line(encode_cue("REMOTE", "ABCD", 1, burst, 3, [record]))
        self.assertEqual(frames.qsize(), 1)
        self.assertIsNotNone(radio._pending_ack)
        self.assertEqual(len(radio._recent), 1)
        self.assertEqual(frames.get_nowait().records, [record])
        radio._handle_line(encode_cue("REMOTE", "ABCD", 2, 1, 3, [record]))
        self.assertEqual(frames.qsize(), 1)
        self.assertEqual(frames.get_nowait().seq, 2)

    def test_received_rssi_is_logged(self):
        receiver = load_script("rssi_log_rx", "LoRaQ-display.py")
        radio = receiver.RadioThread(Mock(), "TEST", 1, queue.Queue(), Mock())
        line = encode_cue("REMOTE", "ABCD", 1, 1, 1, [Record("D", "1", text="GO")])
        with self.assertLogs("loraq.rx", level="INFO") as messages:
            radio._handle_line(line, 0xD2)
        self.assertTrue(any(message.endswith("[RSSI: -46.0 dBm]")
                            and "[RX]" in message
                            for message in messages.output))

    def test_receiver_queues_command_source_and_rssi_only_once(self):
        receiver = load_script("source_metadata_rx", "LoRaQ-display.py")
        frames = queue.Queue()
        radio = receiver.RadioThread(Mock(), "TEST", 1, frames, Mock())
        packets = (encode_cue("FIRST", "ABCD", 1, 1, 1, [Record("C", "0")]),
                   encode_reload("SECOND", "ABCD", 2, 1, 1),
                   encode_osc("THIRD", "ABCD", 3, 1, 1, ""))
        for index, data in enumerate(packets):
            radio._handle_line(data, 0xD2 if index == 0 else None)
            radio._handle_line(data, 0x80)
        radio._handle_line(encode_ack("ACKER", 0, "ABCD", [1]), 0x80)
        radio._handle_line(encode_online("ONLINE", "1.0", 0), 0x80)
        self.assertEqual([(frame.station, frame.rssi) for frame in list(frames.queue)],
                         [("FIRST", 0xD2), ("SECOND", None), ("THIRD", None)])

    def test_range_test_displays_received_rssi_on_third_line(self):
        receiver = load_script("range_test_rssi_rx", "LoRaQ-display.py")
        frames = queue.Queue()
        radio = receiver.RadioThread(Mock(), "TEST", 1, frames, Mock())
        text = "TEST #1\n12:34:56"
        line = encode_cue("REMOTE", "ABCD", 1, 1, 1, [Record("D", "1", text=text)])

        radio._handle_line(line, 0xD2)
        self.assertEqual(frames.get_nowait().records[0].text,
                         f"{text}\nRSSI: -46.0 dBm")

        regular_line = encode_cue("REMOTE", "ABCD", 2, 1, 1, [Record("D", "1", text="GO")])
        radio._handle_line(regular_line, 0xD2)
        self.assertEqual(frames.get_nowait().records[0].text, "GO")

        unavailable_line = encode_cue(
            "REMOTE", "ABCD", 3, 1, 1, [Record("D", "1", text="TEST #2\n12:34:59")])
        radio._handle_line(unavailable_line)
        self.assertEqual(frames.get_nowait().records[0].text,
                         "TEST #2\n12:34:59\nRSSI: unavailable")

    def test_main_loop_visuals_survive_audio_failure_and_duplicate_bursts(self):
        import pygame
        from loraq_audio import CueAudio
        from loraq_gfx_engine import LoRaQEngine
        pygame.font.init()
        receiver = load_script("effects_rx_main", "LoRaQ-display.py")
        engine = LoRaQEngine(pygame.Surface((640, 360)))
        engine.apply_records([Record("D", "1", "G", 30, text="GO")])
        engine.state['area1_start_time'] = 123.0
        engine.render = Mock()
        before = dict(engine.state)
        audio = CueAudio(False)
        channel = Mock()
        channel.play.side_effect = pygame.error("device lost")
        audio.channel = channel
        audio.sounds = {"buzzer": object()}
        args = SimpleNamespace(station_id="TEST", slot=1, port="COM1", interface="usb", windowed=True,
                               debug=False)

        def deliver_bursts(radio):
            for burst in range(1, 4):
                radio._handle_line(encode_cue("REMOTE", "ABCD", 1, burst, 3,
                                              [Record("F", "0", effect_id=3)]))

        with patch.object(receiver, "parse_args", return_value=args), \
                patch.object(receiver, "SerialLink"), \
                patch.object(receiver, "LoRaQEngine", return_value=engine), \
                patch.object(receiver, "CueAudio", return_value=audio), \
                patch.object(receiver.RadioThread, "start", deliver_bursts), \
                patch.object(receiver.RadioThread, "join"), \
                patch.object(receiver, "pygame") as display, \
                self.assertLogs("loraq.audio", level="WARNING"):
            display.event.get.side_effect = [[], [SimpleNamespace(type=display.QUIT)]]
            receiver.main()
        channel.play.assert_called_once()
        self.assertEqual(engine.state, before)
        engine.render.assert_called()
        self.assertIsNone(audio.channel)

    def test_receiver_respects_audio_environment(self):
        with patch.dict(os.environ, SDL_AUDIODRIVER="test-driver"):
            load_script("effects_rx_environment", "LoRaQ-display.py")
            self.assertEqual(os.environ["SDL_AUDIODRIVER"], "test-driver")


class ProtocolEffectTests(unittest.TestCase):
    def test_splitter_overflow_warns_and_recovers(self):
        splitter = LineSplitter()
        sample = b"\x00\xff" * 32
        with self.assertNoLogs("loraq", level="WARNING"):
            self.assertEqual(splitter.feed(sample + b"x" * (splitter.MAX_BUFFER - len(sample))), [])
        with self.assertLogs("loraq", level="WARNING") as captured:
            self.assertEqual(splitter.feed(b"x"), [])
        self.assertEqual(captured.output, [
            "WARNING:loraq:Discarding 1025 bytes of unterminated serial data",
        ])
        frame = encode_online("REMOTE", "1.0", 1)
        self.assertEqual(splitter.feed(frame + b"\xac"), [(frame.strip(), 0xAC)])

    def test_rssi_suffix_sign_extends_appended_byte(self):
        self.assertEqual(rssi_suffix(0xAC), " [RSSI: -84.0 dBm]")
        self.assertEqual(rssi_suffix(0xDC), " [RSSI: -36.0 dBm]")

    def test_rssi_splitter_handles_partial_reads_and_newline_rssi(self):
        splitter = LineSplitter()
        first = encode_cue("REMOTE", "ABCD", 1, 1, 1, [Record("D", "1", text="GO")])
        second = encode_cue("REMOTE", "ABCD", 2, 1, 1, [Record("D", "2", text="NEXT")])
        received = splitter.feed(first + b"\n" + second[:8])
        received.extend(splitter.feed(second[8:] + b"\xff"))
        self.assertEqual([rssi for _, rssi in received], [0x0A, 0xFF])
        self.assertEqual([decode_frame(line).seq for line, _ in received], [1, 2])

    def test_hat_register_config_enables_packet_rssi(self):
        hat = load_script("rssi_hat_config", "LoRaQ-adapter-configure.py")
        registers = hat.build_registers(hat.SETTINGS.radio)
        self.assertEqual(registers[6] & 0x80, 0x80)
        self.assertEqual(registers[6] & 0x7F, 0)

    def test_serial_reads_drain_buffer_and_restore_default_timeout(self):
        import serial
        from loraq_protocol import SerialLink
        link = SerialLink(9600, read_timeout=0.05)
        link._ser = serial.serial_for_url("loop://", timeout=0.05)
        try:
            link._ser.write(b"buffered")
            self.assertEqual(link.read_available(timeout=0.25), b"buffered")
            self.assertEqual(link._ser.timeout, 0.25)
            self.assertEqual(link.read_available(timeout=0.0), b"")
            self.assertEqual(link._ser.timeout, 0.0)
            link._ser.write(b"default")
            self.assertEqual(link.read_available(), b"default")
            self.assertEqual(link._ser.timeout, 0.05)
        finally:
            link.close()

    def test_blocking_serial_read_returns_when_data_arrives(self):
        import serial
        from loraq_protocol import SerialLink
        link = SerialLink(9600, read_timeout=0.25)
        link._ser = serial.serial_for_url("loop://", timeout=0.25)
        started, finished = threading.Event(), threading.Event()
        received = []
        original_read = link._ser.read

        def read(size):
            started.set()
            return original_read(size)

        def receive():
            received.append(link.read_available(timeout=0.25))
            finished.set()

        reader = threading.Thread(target=receive)
        try:
            with patch.object(link._ser, "read", side_effect=read):
                reader.start()
                self.assertTrue(started.wait(1.0))
                link._ser.write(b"X")
                self.assertTrue(finished.wait(0.2))
                reader.join(1.0)
                self.assertEqual(received, [b"X"])
        finally:
            reader.join(1.0)
            link.close()

    def test_serial_read_failure_drops_connection(self):
        import serial
        from loraq_protocol import SerialLink
        link = SerialLink(9600)
        port = Mock(timeout=0.05, in_waiting=0)
        port.read.side_effect = serial.SerialException("unplugged")
        link._ser = port
        with self.assertLogs(level="ERROR"):
            self.assertEqual(link.read_available(timeout=0.25), b"")
        self.assertFalse(link.connected)
        port.close.assert_called_once()

    def test_round_trip_all_effects(self):
        for effect_id in EFFECTS:
            for record in (Record("D", "1", "G", 30, effect_id, "GO|\n^"),
                           Record("F", "0", effect_id=effect_id)):
                with self.subTest(record=record):
                    self.assertEqual(decode_frame(encode_cue("REMOTE", "ABCD", 1, 1, 3, [record])).records,
                                     [record])

    def test_invalid_effect_records_rejected(self):
        for payload in ("D|1|W||8|GO", "D|1|W||-1|GO", "D|1|W||1.0|GO",
                        "D|1|W||01|GO", "F|1|W||1|", "F|0|G||1|",
                        "F|0|W|30|1|", "F|0|W||1|GO", "C|0|W||2|"):
            body = f"C|REMOTE|ABCD|1|1/1|{payload}"
            with self.subTest(payload=payload), self.assertRaises(ProtocolError):
                decode_frame(f"~{body}*{crc16(body)}\r\n".encode())

    def test_encoding_rejects_invalid_effects(self):
        for effect_id in (-1, 8, 1.5, True):
            with self.subTest(effect_id=effect_id), self.assertRaises(ProtocolError):
                Record("D", "1", effect_id=effect_id).encode()

    def test_legacy_wire_values_keep_meaning(self):
        for effect_id in (0, 1):
            record = Record("D", "1", effect_id=effect_id, text="GO")
            self.assertEqual(record.encode(), f"D|1|W||{effect_id}|GO")
            self.assertEqual(EFFECTS[effect_id][0], bool(effect_id))


class StationPacketAndOverlayTests(unittest.TestCase):
    def test_remote_cue_stats_label_includes_station_id(self):
        import pygame
        import loraq_gfx_engine as gfx
        pygame.font.init()
        engine = gfx.LoRaQEngine(pygame.Surface((640, 510)), show_telemetry=True)
        with patch.object(gfx.time, "strftime", return_value="12:34:56"):
            engine.set_remote_cue("STAGE_TX", "ABCD", 42)
        self.assertEqual(engine.remote_cue, "REMOTE CUE STAGE_TX #42 @ 12:34:56")
        engine.render()

    def test_station_packet_validation_and_crc(self):
        for station in ("T", "W" * 16):
            packets = (encode_cue(station, "ABCD", 1, 1, 1, [Record("D", "1", text="GO|^*~\n")]),
                       encode_reload(station, "ABCD", 1, 1, 1),
                       encode_osc(station, "ABCD", 1, 1, 1, '/go "a|b"'))
            for packet in packets:
                frame = decode_frame(packet)
                self.assertEqual(frame.station, station)
                self.assertIsNone(frame.rssi)
                with self.assertRaisesRegex(ProtocolError, "CRC"):
                    decode_frame(packet.replace(station.encode(), b"CHANGED", 1))
        for station in ("", "bad id", "W" * 17, "a|b"):
            for encode in (lambda: encode_cue(station, "ABCD", 1, 1, 1, [Record("C", "0")]),
                           lambda: encode_reload(station, "ABCD", 1, 1, 1),
                           lambda: encode_osc(station, "ABCD", 1, 1, 1, "")):
                with self.subTest(station=station), self.assertRaises(ProtocolError):
                    encode()
        for body in ("C|ABCD|1|1/1|D|1|W||0|GO", "R|ABCD|1|1/1|", "Q|ABCD|1|1/1|/go",
                     "C||ABCD|1|1/1|D|1|W||0|GO", "R|bad id|ABCD|1|1/1|"):
            with self.subTest(body=body), self.assertRaises(ProtocolError):
                decode_frame(f"~{body}*{crc16(body)}\r\n".encode())

    def test_packet_size_checks_include_station_and_utf8(self):
        import loraq_protocol as protocol
        from dataclasses import replace
        station = "W" * 16
        settings = replace(protocol.SETTINGS, station=replace(protocol.SETTINGS.station, station_id=station))
        record = Record("D", "1", text="caf\u00e9")
        command = "/go caf\u00e9"
        with patch.object(protocol, "SETTINGS", settings):
            for fits, payload, packet in ((protocol.frame_fits, [record],
                                          encode_cue(station, "FFFF", protocol.SEQ_MAX, 9, 9, [record])),
                                         (protocol.osc_fits, command,
                                          encode_osc(station, "FFFF", protocol.SEQ_MAX, 9, 9, command))):
                with patch.object(protocol, "MAX_FRAME_BYTES", len(packet)):
                    self.assertTrue(fits(payload))
                with patch.object(protocol, "MAX_FRAME_BYTES", len(packet) - 1):
                    self.assertFalse(fits(payload))

    def test_source_bar_persistence_centering_resize_and_disabled_modes(self):
        import pygame
        import loraq_gfx_engine as gfx
        pygame.font.init()
        for preview in (False, True):
            for enabled in (False, True):
                with self.subTest(preview=preview, enabled=enabled):
                    surface = pygame.Surface((640, 510 if preview else 360))
                    engine = gfx.LoRaQEngine(surface, show_telemetry=preview, show_tx_info=enabled)
                    engine.apply_records([Record("D", "1", text="GO")])
                    before = dict(engine.state)
                    with patch.object(gfx.time, "monotonic", return_value=10.0):
                        engine.set_received_source("W" * 16, 0xD2)
                        engine.render()
                    self.assertEqual(engine.state, before)
                    self.assertEqual(engine.received_source, ("TX: " + "W" * 16, "RSSI: -46.0 dBm"))
                    self.assertEqual(engine._source_visible, enabled)
                    self.assertFalse(engine.has_active_effects(12.99))
                    self.assertFalse(engine.has_active_effects(1000.0))
                    if enabled:
                        self.assertEqual(engine.screen.get_at((0, 0))[:3], gfx.BG_COLOUR)
                        self.assertEqual(engine.screen.get_at((engine.width - 1, 0))[:3], gfx.BG_COLOUR)
                        self.assertLess(engine._source_surface.get_height(), engine.area1_top)
                        self.assertEqual(engine._source_surface.get_width(), engine.width)
                        pixels = [(column, row) for column in range(engine.width)
                                  for row in range(engine.source_bar_height)
                                  if engine._source_surface.get_at((column, row))[:3] != gfx.BG_COLOUR]
                        self.assertTrue(pixels)
                        left = min(column for column, row in pixels)
                        right = max(column for column, row in pixels)
                        self.assertLessEqual(abs(left - (engine.width - 1 - right)), 3)
                    with patch.object(gfx.time, "monotonic", return_value=1000.0):
                        engine.render()
                    self.assertFalse(engine.has_active_effects(1000.01))
                    self.assertEqual(engine._source_visible, enabled)
                    if not enabled:
                        self.assertEqual(engine.screen.get_at((0, 0))[:3], gfx.AREA1_BG)
                    with patch.object(gfx.time, "monotonic", return_value=20.0):
                        engine.set_received_source("NEXT", None)
                        engine.resize(pygame.Surface((400, 320)))
                        engine.trigger_effect(1, gfx.COLOUR_MAP['W'])
                        engine.render()
                    self.assertEqual(engine.received_source[0], "TX: NEXT")
                    self.assertEqual(engine.received_source[1], "RSSI: unavailable")
                    if enabled:
                        self.assertEqual(engine.screen.get_at((0, 0))[:3], gfx.BG_COLOUR)
                        self.assertEqual(engine._source_surface.get_width(), engine.width)
                        if preview:
                            pixel = engine.stage_rect.topleft
                            self.assertEqual(engine.display_surface.get_at(pixel)[:3], gfx.BG_COLOUR)
                    engine.clear_received_source()
                    self.assertFalse(engine._source_active())


if __name__ == "__main__":
    unittest.main()