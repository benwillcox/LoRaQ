"""LoRaQ receiver: full-screen cue display that applies LoRa cue frames and ACKs them in its time slot."""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import platform
import queue
import random
import re
import sys
import threading
import time
from collections import Counter, deque
from dataclasses import dataclass

import pygame

from loraq_audio import CueAudio
from loraq_config import SETTINGS
from loraq_gfx_engine import LoRaQEngine, VERSION
from loraq_icon import set_app_icon
from loraq_webpush import create_pusher, is_test_frame
from loraq_protocol import (
    ACK_GUARD_S, ACK_HISTORY_S, ACK_MAX_SEQS, ACK_SLOT_S, ACK_WINDOW_MARGIN_S, BURST_JITTER_S, BURST_MARGIN_S,
    MAX_SLOT, Deduplicator, LineSplitter, ProtocolError, SerialLink, ack_delay_s, available_ports_hint,
    decode_frame, describe_frame, encode_ack, encode_online, find_lora_port, lora_toa_s, rssi_suffix,
)

BAUD_RATE = SETTINGS.radio.baud_rate
TARGET_FPS = SETTINGS.receiver.target_fps
ACK_SEND_GUARD_S = 0.10  # Keeps consecutive transmissions from this receiver apart on the air
DIAG_INTERVAL_S = 300.0
DIAG_LOOP_STALL_S = 0.5
DIAG_LOG_NAME = "loraq-display.log"

log = logging.getLogger("loraq.rx")


def read_system_health() -> str:
    """Best-effort load, CPU temperature and Raspberry Pi power status; unavailable items are omitted."""
    parts = []
    try:
        parts.append(f"load {os.getloadavg()[0]:.2f}")
    except (AttributeError, OSError):
        pass
    try:
        with open("/sys/class/thermal/thermal_zone0/temp", encoding="ascii") as handle:
            parts.append(f"CPU {int(handle.read().strip()) / 1000:.0f}C")
    except (OSError, ValueError):
        pass
    try:
        with open("/sys/devices/platform/soc/soc:firmware/get_throttled", encoding="ascii") as handle:
            flags = int(handle.read().strip(), 16)
        status = ("UNDER-VOLTAGE NOW" if flags & 0x1 else
                  "under-voltage since boot" if flags & 0x10000 else "power OK")
        parts.append(f"{status} (throttle flags 0x{flags:X})")
    except (OSError, ValueError):
        pass
    return ", ".join(parts) or "system health unavailable"


def start_diagnostic_log(path):
    """Writes the log on a separate thread so a slow SD card can never delay a radio deadline."""
    try:
        handler = logging.handlers.RotatingFileHandler(path, maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    except OSError as e:
        log.warning("Diagnostic log file unavailable (%s); logging to the console only", e)
        return None
    handler.setFormatter(logging.Formatter("%(asctime)s.%(msecs)03d %(levelname)-7s %(name)s: %(message)s",
                                           "%Y-%m-%d %H:%M:%S"))
    records = queue.SimpleQueue()
    logging.getLogger().addHandler(logging.handlers.QueueHandler(records))
    listener = logging.handlers.QueueListener(records, handler)
    listener.start()
    threading.excepthook = lambda args: log.critical(
        "Thread %s crashed", args.thread.name if args.thread else "?",
        exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
    return listener


@dataclass
class PendingAck:
    session: str
    due: float


class RadioThread(threading.Thread):
    """Owns the serial port: decodes frames, de-duplicates bursts and sends slotted ACKs."""

    def __init__(self, link, station_id, slot, frames_out, reload_event, web=None, diagnostics=False):
        super().__init__(name="radio", daemon=True)
        self.web = web
        self.link = link
        self.station_id = station_id
        self.slot = slot
        self.frames_out = frames_out
        self.reload_event = reload_event
        self.diagnostics = diagnostics
        self.stop_event = threading.Event()
        self._splitter = LineSplitter()
        self._dedup = Deduplicator()
        self._recent = deque(maxlen=16)
        self._pending_acks = {}
        self._ack_free_at = 0.0
        self._online_due = None
        self._online_attempted = False
        self._reload_after_ack = None
        self._stats = Counter()
        self._last_burst = None
        self._next_diag = 0.0

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
                self._service_diagnostics()
            except Exception:
                log.exception("Radio loop error")
                self.stop_event.wait(0.1)

    def _read_timeout(self):
        now = time.monotonic()
        timeout = self.link.read_timeout
        if self._online_due is not None:
            timeout = min(timeout, self._online_due - now)
        if self._pending_acks:
            due = max(min(p.due for p in self._pending_acks.values()), self._ack_free_at)
            timeout = min(timeout, due - now)
        return max(0.0, timeout)

    def _service_diagnostics(self):
        now = time.monotonic()
        if not self.diagnostics or now < self._next_diag:
            return
        self._next_diag = now + DIAG_INTERVAL_S
        s = self._stats
        log.info("[DIAG] Health: link %s, %d dongle byte(s) | cues %d (%d duplicate burst(s)), %d unreadable | "
                 "heard %d ACK(s), %d ONLINE | sent %d ACK(s), %d late | %s",
                 "up" if self.link.connected else "DOWN", getattr(self.link, "rx_bytes", 0),
                 s["cues"], s["dups"], s["rejected"], s["acks_heard"], s["online_heard"],
                 s["acks_sent"], s["acks_late"], read_system_health())

    def _log_overheard(self, frame, frame_len, rssi_text):
        self._stats["acks_heard" if frame.kind == "A" else "online_heard"] += 1
        if not self.diagnostics:
            log.debug("Heard %s from station %s%s",
                      "ACK" if frame.kind == "A" else "ONLINE", frame.station, rssi_text)
            return
        if frame.kind == "O":
            log.info("[DIAG] Heard ONLINE from %s (slot %d, v%s)%s", frame.station, frame.slot,
                     frame.version, rssi_text)
            return
        timing = ""
        last = self._last_burst
        if last and last["session"] == frame.session:
            remaining = last["bursts"] - last["burst"]
            expected = (remaining * (lora_toa_s(last["len"]) + BURST_MARGIN_S + BURST_JITTER_S / 2)
                        + ACK_GUARD_S + frame.slot * ACK_SLOT_S + lora_toa_s(frame_len))
            timing = (f" +{(time.monotonic() - last['at']) * 1000:.0f} ms after burst {last['burst']}/"
                      f"{last['bursts']} of Seq #{last['seq']} (slot {frame.slot} expected ~{expected * 1000:.0f} ms)")
        log.info("[DIAG] Heard ACK from %s for Seq %s%s%s", frame.station, ",".join(map(str, frame.acked)),
                 timing, rssi_text)

    def _handle_line(self, line, rssi=None):
        if b"~" not in line:
            self._stats["rejected"] += 1
            log.debug("Ignoring non-LoRaQ serial line: %r", line[:80])
            return
        try:
            frame = decode_frame(line)
        except ProtocolError as e:
            self._stats["rejected"] += 1
            log.warning("[RX REJECTED] %s: %r", e, line[:80])
            return
        frame.rssi = rssi
        rssi_text = rssi_suffix(rssi)
        frame_len = len(line) - line.rfind(b"~") + 2
        if frame.kind in ("A", "O"):
            self._log_overheard(frame, frame_len, rssi_text)
            return

        self._stats["cues"] += 1
        self._last_burst = {"session": frame.session, "seq": frame.seq, "burst": frame.burst,
                            "bursts": frame.bursts, "len": frame_len, "at": time.monotonic()}
        tag = f"Seq #{frame.seq} burst {frame.burst}/{frame.bursts} [TX {frame.station}, session {frame.session}]"
        if not self._dedup.is_new((frame.station, frame.session, frame.seq)):
            self._stats["dups"] += 1
            log.info("[RX DUP] %s already applied%s", tag, rssi_text)
            if frame.session in self._pending_acks:
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
            self._reload_after_ack = frame.session
            log.info("[SYSTEM] RELOAD received; restarting after ACK")

    def _schedule_ack(self, frame, frame_len, now):
        due = now + ack_delay_s(frame_len, frame.burst, frame.bursts, self.slot)
        if self._online_due is not None:
            # Don't announce on top of the ACK slots of a command this receiver has just heard.
            window_end = (now + ack_delay_s(frame_len, frame.burst, frame.bursts, MAX_SLOT)
                          + ACK_SLOT_S + ACK_WINDOW_MARGIN_S)
            self._online_due = max(self._online_due, window_end)
        pending = self._pending_acks.get(frame.session)
        if pending:
            # Another frame arrived before our slot: ACK both together after its bursts end.
            pending.due = max(pending.due, due)
        else:
            if self._pending_acks:
                log.warning("[ACK] Commands from overlapping transmitter sessions; ACKs will be sent one at a time")
            self._pending_acks[frame.session] = PendingAck(frame.session, due)

    def _service_timers(self):
        now = time.monotonic()
        if self._pending_acks and now >= self._ack_free_at:
            pending = min(self._pending_acks.values(), key=lambda p: p.due)
            if now >= pending.due:
                self._send_ack(pending)
                now = time.monotonic()
        if self._online_due is not None and now >= self._online_due:
            if self._pending_acks or now < self._ack_free_at:
                self._online_due = max(now + ACK_WINDOW_MARGIN_S, self._ack_free_at)
            else:
                self._online_due = None
                self._online_attempted = True
                if self.link.write(encode_online(self.station_id, VERSION, self.slot)):
                    log.info("[ONLINE] Written to dongle for %s (v%s, slot %d); RF delivery is not confirmed",
                             self.station_id, VERSION, self.slot)
                else:
                    log.warning("[ONLINE] Startup announcement failed for %s", self.station_id)

    def _send_ack(self, pending):
        del self._pending_acks[pending.session]
        now = time.monotonic()
        if now - pending.due > 0.05:
            self._stats["acks_late"] += 1
            log.warning("[ACK] Slot for session %s was serviced %.0f ms late",
                        pending.session, (now - pending.due) * 1000)
        # Cumulative ACK: repeats recent seqs in case an earlier ACK was lost to a collision.
        seqs = [seq for session, seq, seen in self._recent
                if session == pending.session and now - seen <= ACK_HISTORY_S][-ACK_MAX_SEQS:]
        if seqs:
            data = encode_ack(self.station_id, self.slot, pending.session, seqs)
            if self.link.write(data):
                written_at = time.monotonic()
                self._stats["acks_sent"] += 1
                self._ack_free_at = written_at + lora_toa_s(len(data)) + ACK_SEND_GUARD_S
                log.info("[ACK] Written to dongle for Seq %s (slot %d; RF delivery unconfirmed)",
                         ",".join(map(str, seqs)), self.slot)
                if self.diagnostics:
                    log.info("[DIAG] ACK serviced %+.0f ms vs its slot time; serial write took %.0f ms",
                             (now - pending.due) * 1000, (written_at - now) * 1000)
            else:
                log.warning("[ACK] Write failed for session %s Seq %s", pending.session,
                            ",".join(map(str, seqs)))
        if self._reload_after_ack == pending.session:
            self._reload_after_ack = None
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
                        format="%(asctime)s.%(msecs)03d %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
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
    diagnostics = SETTINGS.station.diagnostic_log
    log_listener = start_diagnostic_log(SETTINGS.path.with_name(DIAG_LOG_NAME)) if diagnostics else None
    if log_listener:
        log.info("[DIAG] Diagnostic log enabled: %s | station %s slot %d | v%s | Python %s, pygame %s, %s | "
                 "ACK guard %.0f ms, slot %.0f ms | %s", SETTINGS.path.with_name(DIAG_LOG_NAME), args.station_id,
                 args.slot, VERSION, platform.python_version(), pygame.version.ver, platform.platform(),
                 ACK_GUARD_S * 1000, ACK_SLOT_S * 1000, read_system_health())
    radio = RadioThread(link, args.station_id, args.slot, frames, reload_event, web, diagnostics=bool(log_listener))
    radio.start()

    clock = pygame.time.Clock()
    running = True
    redraw = True
    last_loop = time.monotonic()
    try:
        while running and not reload_event.is_set():
            loop_start = time.monotonic()
            if log_listener and loop_start - last_loop > DIAG_LOOP_STALL_S:
                log.warning("[DIAG] Display loop stalled for %.0f ms", (loop_start - last_loop) * 1000)
            last_loop = loop_start
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
        if log_listener:
            log_listener.stop()

    if reload_event.is_set():
        os.execv(sys.executable, [sys.executable] + sys.argv)
    return 0


if __name__ == "__main__":
    sys.exit(main())
