"""LoRaQ receiver: full-screen cue display that applies LoRa cue frames and ACKs them in its time slot."""
from __future__ import annotations

import argparse
import logging
import os
import queue
import random
import re
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass

import pygame

from loraq_audio import CueAudio
from loraq_config import SETTINGS
from loraq_gfx_engine import LoRaQEngine, VERSION
from loraq_icon import set_app_icon
from loraq_webpush import create_pusher, is_test_frame
from loraq_protocol import (
    ACK_HISTORY_S, ACK_MAX_SEQS, Deduplicator, LineSplitter, ProtocolError, SerialLink,
    ack_delay_s, available_ports_hint, decode_frame, describe_frame, encode_ack, encode_online,
    find_lora_port, rssi_suffix,
)

BAUD_RATE = SETTINGS.radio.baud_rate
TARGET_FPS = SETTINGS.receiver.target_fps

log = logging.getLogger("loraq.rx")


@dataclass
class PendingAck:
    session: str
    due: float


class RadioThread(threading.Thread):
    """Owns the serial port: decodes frames, de-duplicates bursts and sends slotted ACKs."""

    def __init__(self, link, station_id, slot, frames_out, reload_event, web=None):
        super().__init__(name="radio", daemon=True)
        self.web = web
        self.link = link
        self.station_id = station_id
        self.slot = slot
        self.frames_out = frames_out
        self.reload_event = reload_event
        self.stop_event = threading.Event()
        self._splitter = LineSplitter()
        self._dedup = Deduplicator()
        self._recent = deque(maxlen=16)
        self._pending_ack = None
        self._online_due = None
        self._online_attempted = False
        self._reload_after_ack = False

    def run(self):
        while not self.stop_event.is_set():
            try:
                if not self.link.connected:
                    if not self.link.ensure_open():
                        self.stop_event.wait(0.5)
                        continue
                    self._splitter.reset()
                    if not self._online_attempted:
                        # Random delay so receivers powered up together don't collide
                        self._online_due = time.monotonic() + random.uniform(0.2, 2.0)
                for line, rssi in self._splitter.feed(
                        self.link.read_available(timeout=self._read_timeout())):
                    self._handle_line(line, rssi)
                self._service_timers()
            except Exception:
                log.exception("Radio loop error")
                self.stop_event.wait(0.1)

    def _read_timeout(self):
        now = time.monotonic()
        timeout = self.link.read_timeout
        if self._online_due is not None:
            timeout = min(timeout, self._online_due - now)
        if self._pending_ack is not None:
            timeout = min(timeout, self._pending_ack.due - now)
        return max(0.0, timeout)

    def _handle_line(self, line, rssi=None):
        if b"~" not in line:
            log.debug("Ignoring non-LoRaQ serial line: %r", line[:80])
            return
        try:
            frame = decode_frame(line)
        except ProtocolError as e:
            log.warning("[RX REJECTED] %s: %r", e, line[:80])
            return
        frame.rssi = rssi
        rssi_text = rssi_suffix(rssi)
        if frame.kind in ("A", "O"):
            log.debug("Heard %s from station %s%s",
                      "ACK" if frame.kind == "A" else "ONLINE", frame.station, rssi_text)
            return

        tag = f"Seq #{frame.seq} burst {frame.burst}/{frame.bursts} [TX {frame.station}, session {frame.session}]"
        if not self._dedup.is_new((frame.session, frame.seq)):
            log.info("[RX DUP] %s already applied%s", tag, rssi_text)
            if self._pending_ack and self._pending_ack.session == frame.session:
                self._schedule_ack(frame, len(line) - line.rfind(b"~") + 2, time.monotonic())
            return

        log.info("[RX] %s: %s%s", tag, describe_frame(frame), rssi_text)
        if frame.kind == "C":
            test_frame = is_test_frame(frame)
            if self.web and not test_frame:
                self.web.submit(frame.station, frame.session, frame.seq, frame.records)
            rssi_line = rssi_text.strip()[1:-1] if rssi_text else "RSSI: unavailable"
            for record in frame.records:
                if (test_frame and record.op == "D" and record.area == "1"
                        and re.fullmatch(r"TEST #\d+\n\d{2}:\d{2}:\d{2}", record.text)):
                    record.text += f"\n{rssi_line}"
        now = time.monotonic()
        self._recent.append((frame.session, frame.seq, now))
        self.frames_out.put(frame)
        self._schedule_ack(frame, len(line) - line.rfind(b"~") + 2, now)
        if frame.kind == "R":
            self._reload_after_ack = True
            log.info("[SYSTEM] RELOAD received; restarting after ACK")

    def _schedule_ack(self, frame, frame_len, now):
        due = now + ack_delay_s(frame_len, frame.burst, frame.bursts, self.slot)
        pending = self._pending_ack
        if pending and pending.session != frame.session:
            self._send_ack()
            pending = None
        if pending:
            # Another frame arrived before our slot: ACK both together after its bursts end.
            pending.due = max(pending.due, due)
        else:
            self._pending_ack = PendingAck(frame.session, due)

    def _service_timers(self):
        now = time.monotonic()
        if self._online_due is not None and now >= self._online_due:
            self._online_due = None
            self._online_attempted = True
            if self.link.write(encode_online(self.station_id, VERSION, self.slot)):
                log.info("[ONLINE] Written to dongle for %s (v%s, slot %d); RF delivery is not confirmed",
                         self.station_id, VERSION, self.slot)
            else:
                log.warning("[ONLINE] Startup announcement failed for %s", self.station_id)
        if self._pending_ack and now >= self._pending_ack.due:
            self._send_ack()

    def _send_ack(self):
        pending, self._pending_ack = self._pending_ack, None
        now = time.monotonic()
        # Cumulative ACK: repeats recent seqs in case an earlier ACK was lost to a collision.
        seqs = [seq for session, seq, seen in self._recent
                if session == pending.session and now - seen <= ACK_HISTORY_S][-ACK_MAX_SEQS:]
        if seqs and self.link.write(encode_ack(self.station_id, self.slot, pending.session, seqs)):
            log.info("[ACK] Written to dongle for Seq %s (slot %d; RF delivery unconfirmed)",
                 ",".join(map(str, seqs)), self.slot)
        if self._reload_after_ack:
            self._reload_after_ack = False
            self.reload_event.set()


def parse_args():
    parser = argparse.ArgumentParser(description="LoRaQ receiver display")
    parser.add_argument("--port", default=None,
                        help="serial port, e.g. /dev/serial0 or COM4 (default: $LORAQ_PORT, [link] port, auto-detect)")
    parser.add_argument("--interface", choices=("auto", "usb", "hat"), default=SETTINGS.link.interface,
                        help="LoRa connection type (default: [link] interface)")
    parser.add_argument("--windowed", action="store_true", help="run in a 1280x720 window instead of full screen")
    parser.add_argument("--debug", action="store_true", help="verbose logging")
    args = parser.parse_args()
    args.station_id = SETTINGS.station.station_id
    args.slot = SETTINGS.station.ack_slot
    return args


def main():
    args = parse_args()
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    log.info("LoRaQ Receiver v%s | station %s | ACK slot %d | %d FPS", VERSION, args.station_id, args.slot, TARGET_FPS)
    log.info("Config %s | SF%d BW%g kHz CR 4/%d | ACK slot %d ms", SETTINGS.path,
             SETTINGS.radio.spreading_factor, SETTINGS.radio.bandwidth_khz, SETTINGS.radio.coding_rate + 4,
             round(SETTINGS.timing.ack_slot_s * 1000))
    log.info("Link: %s interface | port %s", args.interface,
             args.port or find_lora_port(args.interface) or f"not found (available: {available_ports_hint()})")

    pygame.display.init()
    pygame.font.init()
    set_app_icon("LoRaQ.Receiver")
    if args.windowed:
        screen = pygame.display.set_mode((1280, 720))
    else:
        pygame.mouse.set_visible(False)
        info = pygame.display.Info()
        screen = pygame.display.set_mode((info.current_w, info.current_h), pygame.FULLSCREEN)
    pygame.display.set_caption(f"LoRaQ Receiver - {args.station_id}")

    engine = LoRaQEngine(screen, station_id=args.station_id, ack_slot=args.slot,
                         show_tx_info=SETTINGS.receiver.show_tx_info)
    audio = CueAudio(SETTINGS.receiver.audio_enabled)
    frames = queue.Queue()
    reload_event = threading.Event()
    link = SerialLink(BAUD_RATE, port=args.port, read_timeout=0.25, interface=args.interface)
    web = create_pusher(SETTINGS.web_push, args.station_id, VERSION)
    radio = RadioThread(link, args.station_id, args.slot, frames, reload_event, web)
    radio.start()

    clock = pygame.time.Clock()
    running = True
    redraw = True
    try:
        while running and not reload_event.is_set():
            if engine.set_link_status(link.connected, link.last_error):
                redraw = True
            for event in pygame.event.get():
                if event.type == pygame.QUIT or (event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE):
                    running = False
                elif event.type == pygame.VIDEOEXPOSE:
                    redraw = True

            while True:
                try:
                    frame = frames.get_nowait()
                except queue.Empty:
                    break
                engine.set_received_source(frame.station, frame.rssi)
                redraw = True
                if frame.kind == "C":
                    try:
                        engine.apply_records(frame.records)
                        audio.play_records(frame.records)
                        redraw = True
                    except Exception:
                        log.exception("Failed to apply Seq #%d", frame.seq)

            if redraw or engine.has_active_effects(time.monotonic()):
                engine.render()
                pygame.display.flip()
                redraw = False
            clock.tick(TARGET_FPS)
    except KeyboardInterrupt:
        pass
    finally:
        radio.stop_event.set()
        radio.join(timeout=1.0)
        if web:
            web.stop()
        link.close()
        audio.close()
        pygame.quit()

    if reload_event.is_set():
        log.info("[SYSTEM] Restarting application process...")
        os.execv(sys.executable, [sys.executable] + sys.argv)
    return 0


if __name__ == "__main__":
    sys.exit(main())
