"""LoRaQ transmitter: OSC / CLI cue gateway that broadcasts cues over LoRa and monitors receiver ACKs.

Usage:
  python LoRaQ-transmitter.py                      Interactive console + OSC server + preview
  python LoRaQ-transmitter.py "<Text>" [options]   One-shot cue (forwarded to a running transmitter if present)
    python LoRaQ-transmitter.py oscsend                    Trigger configured OSC command over LoRa
  Add --debug to any mode for verbose logging.
"""
from __future__ import annotations

import logging
import logging.handlers
import queue
import random
import re
import shlex
import socket
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field

try:
    import readline  # noqa: F401  (enables line editing/history in input())
except ImportError:
    pass

from pythonosc import dispatcher, osc_server, udp_client

from loraq_config import SETTINGS
from loraq_webpush import create_pusher, is_test_frame
from loraq_protocol import (
    ACK_HISTORY_S, ACK_MAX_SEQS, ACK_GUARD_S, ACK_SLOT_S, ACK_WINDOW_MARGIN_S, APP_VERSION, COLOUR_LETTERS,
    EFFECTS, MAX_FRAME_BYTES, SEQ_MAX, LineSplitter, Deduplicator,
    ProtocolError, Record, SerialLink, ack_delay_s, ack_window_s, available_ports_hint,
    burst_gap_s, decode_frame, describe_frame, describe_records, encode_ack, encode_cue, encode_osc, encode_reload,
    find_lora_port, frame_fits, lora_toa_s, osc_fits, parse_osc_command, parse_timer, rssi_suffix,
)

# Optional graphics engine (graceful fallback to headless if pygame is missing)
GFX_AVAILABLE = False
try:
    import pygame
    from loraq_gfx_engine import LoRaQEngine, PREVIEW_STATS_HEIGHT
    from loraq_icon import set_app_icon
    GFX_AVAILABLE = True
except ImportError:
    LoRaQEngine = None

try:
    from loraq_audio import CueAudio
except ImportError:
    CueAudio = None

BAUD_RATE = SETTINGS.radio.baud_rate
OSC_IP = SETTINGS.transmitter.osc_ip
OSC_PORT = SETTINGS.transmitter.osc_port
OSC_FORWARD_IP = SETTINGS.transmitter.osc_forward_ip
OSC_FORWARD_PORT = SETTINGS.transmitter.osc_forward_port
DEFAULT_BURSTS = 1 + SETTINGS.transmitter.retries
TEST_BURSTS = 1 + SETTINGS.transmitter.test_retries
TEST_INTERVAL_S = SETTINGS.transmitter.test_interval_s
COALESCE_WINDOW_SEC = SETTINGS.transmitter.coalesce_window_s
EXPECTED_STATIONS = SETTINGS.expected_stations
MAX_STATION_SLOTS = SETTINGS.transmitter.min_ack_slots
MISS_ALERT_THRESHOLD = 1
# Consecutive cue windows in which the dongle returned no bytes at all before the connection is reopened.
RX_SILENT_WINDOWS = 3
RX_RECOVERY_INTERVAL_S = 60.0
SUPERVISE_INTERVAL_S = 1.0
DUTY_CYCLE_LIMIT_PCT = SETTINGS.transmitter.duty_cycle_limit_pct
PREVIEW_WIDTH = SETTINGS.transmitter.preview_width
PREVIEW_HEIGHT = SETTINGS.transmitter.preview_height

PROMPT = "LoRaQ Tx > "

COLOUR_NAMES = {"white": "W", "red": "R", "green": "G", "yellow": "Y", "blue": "B", "orange": "O"}
COLOUR_NAMES.update({code.lower(): code for code in COLOUR_LETTERS})
COLOUR_CYCLE = ["R", "G", "Y", "B", "O", "W"]

HELP_TEXT = fr"""--------------------------------------------------
 SYNTAX:
    "<Text>" [Area: 1|2] [Colour: white|red|green|yellow|blue|orange] [Timer: MM:SS] [Effect: 0-7]
    <Text>|<Area>|<Colour>|<Timer>|<Effect>    (pipe syntax; TIMER also accepts seconds)
  Use \n inside text for a line break.
--------------------------------------------------
 CONSOLE COMMAND EXAMPLES:
  "SIGNAL RED" 1 red              -> Multi-word text (double quotes required)
  "DIRECTOR'S CUE" 1 yellow 1:30  -> Text with apostrophes and a timer
  "CUE 42" 2 white                -> Send to Area 2
  "GO" 1 green 00:30 2            -> Timer plus flash and buzzer
  alert 6                         -> Flash and double-beep on the existing screen
  clear [1|2]                     -> Clear both areas (default) or area 1/2 only
  reload                          -> Restart all receiver applications
  test                            -> Toggle range test ({TEST_INTERVAL_S:g}-second interval)
  oscsend                         -> Trigger the configured OSC command over LoRa
  oscstandby <OSC command>        -> Configure and arm OSC forwarding
  oscstandby 0                    -> Disarm OSC forwarding
  help                            -> Show this help
  exit                            -> Shut down the transmitter
--------------------------------------------------
 OSC ADDRESSES:
  /display <cue args>              -> Send a cue (same syntax as the console)
  /clear [1|2]                     -> Clear both areas or area 1/2 only
  /alert <id>                      -> Apply an effect without changing text or timers
  /oscsend                         -> Trigger the configured OSC command over LoRa
  /oscstandby <OSC command>        -> Configure and arm OSC forwarding
  /oscstandby 0                    -> Disarm OSC forwarding
--------------------------------------------------
 EFFECT IDS:
  0 none; 1 flash; 2 flash+buzzer; 3 buzzer;
  4 flash+beep; 5 beep; 6 flash+double-beep; 7 double-beep.
  Positional timers require MM:SS.
--------------------------------------------------
 ACK MONITORING:
  After each cue a summary shows which stations acknowledged it.
  A missed ACK is a WARNING; {MISS_ALERT_THRESHOLD} in a row raises an ALERT (check power / RF).
=================================================="""

log = logging.getLogger("loraq.tx")


# ----------------------------------------------------------------------
# Console logging that doesn't trample the input prompt
# ----------------------------------------------------------------------
class PromptAwareHandler(logging.StreamHandler):
    prompt_active = False

    def emit(self, record):
        try:
            msg = self.format(record)
            if self.prompt_active:
                self.stream.write("\r" + msg.ljust(len(PROMPT)) + "\n" + PROMPT)
            else:
                self.stream.write(msg + "\n")
            self.flush()
        except Exception:
            self.handleError(record)


console_handler = PromptAwareHandler(sys.stdout)


def setup_logging(debug):
    console_handler.setFormatter(logging.Formatter("%(asctime)s.%(msecs)03d %(levelname)-7s %(message)s", "%H:%M:%S"))
    root = logging.getLogger()
    root.addHandler(console_handler)
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    threading.excepthook = lambda args: log.critical(
        "Thread %s crashed", args.thread.name if args.thread else "?",
        exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
    if not SETTINGS.station.diagnostic_log:
        return
    # With [station] diagnostic_log = true, a log file next to loraq.ini keeps evidence of faults that happen
    # while nobody is watching the console.
    try:
        file_handler = logging.handlers.RotatingFileHandler(
            SETTINGS.path.with_name("loraq-transmitter.log"), maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    except OSError as e:
        log.warning("Diagnostic log file unavailable (%s); logging to the console only", e)
        return
    file_handler.setFormatter(logging.Formatter("%(asctime)s.%(msecs)03d %(levelname)-7s %(message)s",
                                                "%Y-%m-%d %H:%M:%S"))
    root.addHandler(file_handler)
    log.info("Diagnostic log enabled: %s", file_handler.baseFilename)


# ----------------------------------------------------------------------
# Cue parsing & validation (shared by console, OSC and one-shot)
# ----------------------------------------------------------------------
class CueError(ValueError):
    pass


@dataclass
class TxItem:
    kind: str  # "cue", "test", "reload" or "osc"
    records: list[Record] = field(default_factory=list)
    osc: str = ""

    def zones(self) -> set[str]:
        zones = set()
        for rec in self.records:
            zones |= rec.zones()
        return zones


def _arg_str(value) -> str:
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).strip()


def build_item(raw_args) -> TxItem:
    if raw_args:
        command = str(raw_args[0]).strip().lower()
        if command == "osc":
            raise CueError("Use 'oscsend' for a one-shot OSC relay command.")
        if command == "oscsend":
            return _osc_item(raw_args[1:])
    if raw_args and str(raw_args[0]).strip().lower() in ("alert", "/alert"):
        return _effect_item(raw_args[1:])
    args = [_arg_str(a) for a in raw_args]
    if not args:
        raise CueError("No cue given")
    first = args[0].lower()
    if first in ("clear", "cls"):
        return _clear_item(args[1:])
    if first in ("reload", "restart"):
        return TxItem("reload")
    if len(args) == 1 and "|" in args[0]:
        return _pipe_item(args[0])
    return _positional_item(args)


def _osc_item(raw_args) -> TxItem:
    if raw_args:
        raise CueError("Use '/oscstandby <OSC command>' to configure the command; oscsend takes no parameters.")
    command = ""
    if not osc_fits(command):
        raise CueError(f"OSC command is too long for one LoRa frame ({MAX_FRAME_BYTES} bytes). Shorten it.")
    return TxItem("osc", osc=command)


def _osc_command(raw_args) -> str:
    # repr keeps typed OSC floats such as 1.0 as floats on the far side.
    command = shlex.join(repr(a) if isinstance(a, float) else str(a) for a in raw_args)
    try:
        parse_osc_command(command)
    except ValueError as e:
        raise CueError(str(e)) from None
    if not osc_fits(command):
        raise CueError(f"OSC command is too long for one LoRa frame ({MAX_FRAME_BYTES} bytes). Shorten it.")
    return command


def _clear_item(rest) -> TxItem:
    area = rest[0].lower() if rest else "0"
    area = {"all": "0", "both": "0"}.get(area, area)
    if area not in ("0", "1", "2"):
        raise CueError(f"Invalid clear area '{rest[0]}'. Use 1, 2, or omit for both.")
    return TxItem("cue", [Record("C", area)])


def _pipe_item(raw) -> TxItem:
    parts = [p.strip() for p in raw.split("|")]
    if len(parts) > 5:
        raise CueError("Too many fields. Pipe syntax is TEXT|AREA|COLOUR|TIMER|EFFECT")
    text, area, colour, timer, effect = parts + [""] * (5 - len(parts))
    return _display_item(text, area or "1", colour or "W", timer, _effect_id(effect or "0"))


def _effect_id(value) -> int:
    text = str(value).strip()
    if text not in {str(effect_id) for effect_id in EFFECTS}:
        raise CueError(f"Invalid effect ID '{text}'. Use an integer from 0 to 7.")
    return int(text)


def _effect_item(args) -> TxItem:
    if len(args) != 1:
        raise CueError("Use /alert <id> with exactly one effect ID (0-7).")
    return TxItem("cue", [Record("F", "0", effect_id=_effect_id(args[0]))])


def _positional_item(args) -> TxItem:
    text, tokens = args[0], list(args[1:])
    area, colour, timer, effect_id = "1", "W", "", None
    if tokens and tokens[0] in ("1", "2"):
        area = tokens.pop(0)
    if tokens and tokens[0].lower() in COLOUR_NAMES:
        colour = tokens.pop(0)
    for tok in tokens:
        if not tok:
            continue
        if re.fullmatch(r"[0-9]", tok):
            if effect_id is not None:
                raise CueError("Only one effect ID is allowed")
            effect_id = _effect_id(tok)
        elif re.fullmatch(r"[0-9]{1,2}:[0-9]{2}", tok):
            if timer:
                raise CueError("Only one timer is allowed")
            timer = tok
        else:
            raise CueError(f"Unrecognised option '{tok}'. Use MM:SS for timers and 0-7 for effects.")
    return _display_item(text, area, colour, timer, effect_id or 0)


def _display_item(text, area, colour, timer, effect_id) -> TxItem:
    if area not in ("1", "2"):
        raise CueError(f"Invalid area '{area}'. Use 1 or 2.")
    code = COLOUR_NAMES.get(colour.lower())
    if code is None:
        raise CueError(f"Unknown colour '{colour}'. Use white, red, green, yellow, blue or orange.")
    seconds = None
    if timer:
        try:
            seconds = parse_timer(timer)
        except ValueError as e:
            raise CueError(str(e)) from None
    record = Record("D", area, code, seconds, effect_id, text.replace("\\n", "\n"))
    if not frame_fits([record]):
        raise CueError(f"Cue text is too long for one LoRa frame ({MAX_FRAME_BYTES} bytes). Shorten it.")
    return TxItem("cue", [record])


# ----------------------------------------------------------------------
# ACK Monitoring (indication only - nothing is re-sent automatically)
# ----------------------------------------------------------------------
@dataclass
class StationStatus:
    slot: int | None = None
    version: str = ""
    state: str = "UNKNOWN"  # UNKNOWN, ONLINE, OK, MISSED, NO_RESPONSE
    misses: int = 0
    ack_seq: int | None = None
    ack_rssi: int | None = None
    online_since_ack: bool = False

    def label(self, sent_seq: int | None) -> str:
        ack_rssi = rssi_suffix(self.ack_rssi)
        ack = f"ACK #{self.ack_seq}{ack_rssi}" if self.ack_seq is not None else "no ACK"
        last = f" / last ACK #{self.ack_seq}{ack_rssi}" if self.ack_seq is not None else ""
        summary = {
            "OK": ack if self.ack_seq == sent_seq else f"{ack} / waiting #{sent_seq}",
            "ONLINE": "waiting for ACK",
            "MISSED": f"ACK MISSING x{self.misses}{last} / TX #{sent_seq}",
            "NO_RESPONSE": f"NO ACK x{self.misses}{last} / TX #{sent_seq}",
        }.get(self.state, "WAITING / no ACK")
        return f"ONLINE v{self.version} / {summary}" if self.online_since_ack else summary


@dataclass
class AckWindow:
    seq: int
    label: str
    deadline: float
    expected: set[str]
    acked: set[str] = field(default_factory=set)
    extended: bool = False


class AckTracker:
    def __init__(self, session, on_change=None, excluded_station=None):
        self.session = session
        self._on_change = on_change
        self._lock = threading.Lock()
        self._stations = {sid: StationStatus(slot=slot) for sid, slot in EXPECTED_STATIONS.items()
                          if sid != excluded_station}
        self._open: dict[int, AckWindow] = {}
        self._issued: dict[int, float] = {}
        self._timing: dict[int, tuple[float, float]] = {}
        self._sent_seq: int | None = None

    def slot_count(self) -> int:
        with self._lock:
            slots = [s.slot for s in self._stations.values() if s.slot is not None]
        return max([MAX_STATION_SLOTS] + [slot + 1 for slot in slots])

    def prepare_window(self, seq, label):
        with self._lock:
            previous = self._sent_seq
            now = time.monotonic()
            self._issued = {key: issued_at for key, issued_at in self._issued.items()
                            if key in self._open or now - issued_at < 120.0}
            self._issued[seq] = now
            self._open[seq] = AckWindow(seq, label, float("inf"), set(self._stations))
            self._sent_seq = seq
            return previous

    def cancel_window(self, seq, previous):
        with self._lock:
            self._open.pop(seq, None)
            self._issued.pop(seq, None)
            self._sent_seq = previous
        self._notify()

    def open_window(self, seq, label, deadline, last_write=None, toa=0.0):
        with self._lock:
            # Receivers fold earlier seqs into their next ACK, so an open window gets one extension.
            for window in self._open.values():
                if window.seq != seq and not window.extended and deadline > window.deadline:
                    window.deadline, window.extended = deadline, True
            if seq in self._open:
                self._open[seq].deadline = deadline
            else:
                self._open[seq] = AckWindow(seq, label, deadline, set(self._stations))
                self._issued[seq] = time.monotonic()
            if last_write is not None:
                self._timing[seq] = (last_write, toa)
            self._issued = {key: issued_at for key, issued_at in self._issued.items()
                            if key in self._open or time.monotonic() - issued_at < 120.0}
            self._timing = {key: value for key, value in self._timing.items() if key in self._issued}
            self._sent_seq = seq
        self._notify()

    def wait_closed(self, timeout):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            with self._lock:
                if not self._open:
                    return
            time.sleep(0.05)

    def on_online(self, station, version, slot, rssi=None):
        with self._lock:
            status = self._stations.get(station)
            reannounced = status is not None and bool(status.version)
            if status is None:
                status = self._stations[station] = StationStatus()
            status.slot, status.version, status.online_since_ack = slot, version, True
            prev_misses = status.misses
            if status.state == "UNKNOWN":
                status.state = "ONLINE"
            # A freshly (re)started receiver can't ACK cues sent before it came up.
            for window in self._open.values():
                window.expected.discard(station)
            clashes = sorted(sid for sid, s in self._stations.items() if sid != station and s.slot == slot)
        if reannounced:
            log.warning("Station %s re-announced ONLINE (v%s, slot %d); possible reboot or serial reconnect%s",
                        station, version, slot, rssi_suffix(rssi))
        else:
            log.info("Station %s ONLINE (v%s, ACK slot %d)%s",
                     station, version, slot, rssi_suffix(rssi))
        if prev_misses:
            log.info("Station %s ONLINE after %d missed cue(s); ACK still required", station, prev_misses)
        if clashes:
            log.warning("ACK slot %d is shared by %s and %s; set a unique [station] ack_slot",
                        slot, station, ", ".join(clashes))
        self._notify()

    def on_ack(self, station, session, seqs, rssi=None, slot=None, frame_len=None):
        received_at = time.monotonic()
        if session != self.session:
            log.debug("Ignoring ACK from %s for another transmitter session %s%s",
                      station, session, rssi_suffix(rssi))
            return
        with self._lock:
            valid_seqs = [seq for seq in seqs if seq in self._issued]
            if not valid_seqs:
                log.debug("Ignoring ACK from %s for unknown/expired Seq %s%s",
                          station, seqs, rssi_suffix(rssi))
                return
            status = self._stations.get(station)
            discovered = status is None
            if discovered:
                status = self._stations[station] = StationStatus()
            previous_slot = status.slot
            if slot is not None:
                status.slot = slot
            clashes = (sorted(sid for sid, other in self._stations.items()
                              if sid != station and other.slot == slot)
                       if slot is not None and (discovered or previous_slot != slot) else [])
            prev_state, prev_misses = status.state, status.misses
            for seq in valid_seqs:
                window = self._open.get(seq)
                if window:
                    window.acked.add(station)
            latest_ack = max(valid_seqs, key=self._issued.__getitem__)
            if status.ack_seq is None or self._issued[latest_ack] >= self._issued.get(status.ack_seq, -1):
                status.ack_seq = latest_ack
                status.ack_rssi = rssi
            current = self._sent_seq in valid_seqs
            if current:
                status.state, status.misses = "OK", 0
                status.online_since_ack = False
            timing_note = self._ack_timing_note(latest_ack, status.slot, frame_len, received_at)
        if discovered:
            log.info("Station %s discovered via ACK", station)
        if clashes:
            log.warning("ACK slot %d is shared by %s and %s; set a unique [station] ack_slot",
                        slot, station, ", ".join(clashes))
        if current and prev_state in ("MISSED", "NO_RESPONSE"):
            log.info("Station %s RECOVERED after %d missed cue(s)", station, prev_misses)
        log.info("ACK from %s for Seq %s%s", station, ",".join(map(str, valid_seqs)),
                 ("" if current else f" (latest sent Seq #{self._sent_seq} still unconfirmed)")
                 + timing_note + rssi_suffix(rssi))
        self._notify()

    def _ack_timing_note(self, seq, slot, frame_len, received_at):
        """Describes when an ACK arrived relative to where its slot should put it (caller holds the lock)."""
        timing = self._timing.get(seq)
        if timing is None or slot is None or frame_len is None:
            return ""
        last_write, toa = timing
        actual = received_at - last_write
        expected = toa + ACK_GUARD_S + slot * ACK_SLOT_S + lora_toa_s(frame_len)
        closed = " AFTER ITS WINDOW CLOSED" if seq not in self._open else ""
        return f" +{actual * 1000:.0f} ms after last burst (slot {slot} expected ~{expected * 1000:.0f} ms){closed}"

    def poll(self):
        now = time.monotonic()
        with self._lock:
            closed = sorted((w for w in self._open.values() if now >= w.deadline), key=lambda w: w.seq)
            misses = []
            for window in closed:
                del self._open[window.seq]
                for sid in sorted(window.expected - window.acked):
                    status = self._stations[sid]
                    if (status.ack_seq is not None and status.ack_seq in self._issued
                            and self._issued[status.ack_seq] > self._issued[window.seq]):
                        continue
                    status.misses += 1
                    status.state = "NO_RESPONSE" if status.misses >= MISS_ALERT_THRESHOLD else "MISSED"
                    misses.append((window.seq, sid, status.misses, status.state))

        for window in closed:
            missing = sorted(window.expected - window.acked)
            parts = [f"{sid} OK" for sid in sorted(window.acked)] + [f"{sid} MISSING" for sid in missing]
            log.info("Seq #%d ACK: %s", window.seq, ", ".join(parts) if parts else "no stations known yet")
        for seq, sid, count, state in misses:
            if state == "NO_RESPONSE":
                log.error("ALERT: %s NO ACK for %d consecutive cues (delivery unconfirmed; check power / RF)", sid, count)
            else:
                log.warning("%s ACK not received for Seq #%d (%d consecutive; cue delivery unconfirmed)", sid, seq, count)
        if closed:
            self._notify()
        return closed

    def snapshot(self):
        with self._lock:
            return [(sid, s.slot, "UNKNOWN" if s.state == "OK" and s.ack_seq != self._sent_seq else s.state,
                     s.label(self._sent_seq)) for sid, s in sorted(self._stations.items())]

    def _notify(self):
        if self._on_change:
            self._on_change(self.snapshot())


class OscStandby:
    """Safety interlock: forwards exactly one LoRa-relayed OSC command per arming."""

    def __init__(self, ui):
        self._ui = ui
        self._lock = threading.Lock()
        self._armed = False
        self._forwarded = False
        self._command = ""

    @property
    def armed(self):
        return self._armed

    def arm(self, source, command):
        if not OSC_FORWARD_IP:
            log.error("%s: OSC standby refused; set osc_forward_ip/osc_forward_port in [transmitter]", source)
            return
        try:
            parse_osc_command(command)
        except ValueError as e:
            raise CueError(str(e)) from None
        with self._lock:
            self._armed = True
            self._forwarded = False
            self._command = command
        log.info("OSC STANDBY armed (%s): %s will be forwarded to %s:%d",
                 source, command, OSC_FORWARD_IP, OSC_FORWARD_PORT)
        self._ui("osc_status", "STANDBY", f"OSC STANDBY - waiting to forward {command} to {OSC_FORWARD_IP}:{OSC_FORWARD_PORT}")

    def disarm(self, source):
        with self._lock:
            was_armed, self._armed = self._armed, False
            self._command = ""
        if was_armed:
            log.info("OSC standby disarmed (%s)", source)
            self._ui("osc_status", "IDLE", "")

    def clear_forwarded(self):
        with self._lock:
            if self._forwarded:
                self._forwarded = False
                self._ui("osc_status", "IDLE", "")

    def handle(self, frame, rssi=None):
        with self._lock:
            armed, self._armed = self._armed, False
            command, self._command = self._command, ""
        stamp = time.strftime("%H:%M:%S")
        if not armed:
            log.warning("Remote OSC command IGNORED (not in standby; session %s): %r%s",
                        frame.session, frame.osc, rssi_suffix(rssi))
            self._ui("osc_status", "IGNORED", f"OSC IGNORED (not armed) {frame.osc!r} @ {stamp}")
            return
        try:
            address, args = parse_osc_command(command)
        except ValueError as e:
            log.error("Configured OSC command rejected (session %s): %s%s",
                      frame.session, e, rssi_suffix(rssi))
            return
        try:
            udp_client.SimpleUDPClient(OSC_FORWARD_IP, OSC_FORWARD_PORT).send_message(address, args)
        except OSError as e:
            log.error("OSC forward to %s:%d FAILED: %s%s",
                      OSC_FORWARD_IP, OSC_FORWARD_PORT, e, rssi_suffix(rssi))
            self._ui("osc_status", "ERROR", f"OSC FORWARD FAILED {frame.osc!r} @ {stamp}")
            return
        log.info("Forwarded configured OSC command to %s:%d (session %s Seq #%d): %s %s; standby disarmed%s",
             OSC_FORWARD_IP, OSC_FORWARD_PORT, frame.session, frame.seq, address, args, rssi_suffix(rssi))
        with self._lock:
            self._forwarded = True
            self._ui("osc_status", "FORWARDED", f"OSC FORWARDED {command!r} @ {stamp}")


class DutyCycleMeter:
    WINDOW_S = 3600.0

    def __init__(self):
        self._events = deque()

    def add(self, airtime_s):
        now = time.monotonic()
        self._events.append((now, airtime_s))
        while now - self._events[0][0] > self.WINDOW_S:
            self._events.popleft()
        total = sum(airtime for _, airtime in self._events)
        return total / self.WINDOW_S * 100.0, total


# ----------------------------------------------------------------------
# Transmitter core: FIFO TX worker, RX reader and ACK monitor threads
# ----------------------------------------------------------------------
class Transmitter:
    def __init__(self, link, on_ui=None, preview_enabled=False, web=None):
        self.link = link
        self.web = web
        self.session = f"{random.getrandbits(16):04X}"
        self.queue = queue.Queue()
        self.stop_event = threading.Event()
        self.failures = 0
        self._on_ui = on_ui
        self.preview_enabled = preview_enabled
        self.station_id = SETTINGS.station.station_id
        self.ack_slot = SETTINGS.station.ack_slot
        self._radio_lock = threading.RLock()
        self._radio_changed = threading.Condition(self._radio_lock)
        self._transmitting = False
        self._radio_busy_until = 0.0
        self._remote_free_at = 0.0
        self._pending_acks = {}
        self._recent_remote = deque(maxlen=16)
        self.tracker = AckTracker(self.session, on_change=lambda snapshot: self._ui("stations", snapshot),
                                  excluded_station=self.station_id if preview_enabled else None)
        self.osc_standby = OscStandby(self._ui)
        self._duty = DutyCycleMeter()
        self._seq = 0
        self._channel_free_at = 0.0
        self._threads = {}
        self._last_supervised = 0.0
        self._rx_at_send = {}
        self._rx_rejected = 0
        self._silent_windows = 0
        self._last_rx_recovery = None

    def start(self):
        for name in ("tx-worker", "rx-reader", "ack-monitor"):
            self._spawn(name)
        self._ui("stations", self.tracker.snapshot())

    def _spawn(self, name):
        target = {"tx-worker": self._worker, "rx-reader": self._reader, "ack-monitor": self._monitor}[name]
        thread = threading.Thread(target=target, name=name, daemon=True)
        self._threads[name] = thread
        thread.start()

    def _supervise(self):
        """A dead worker or reader would leave the transmitter half-working (e.g. sending but deaf)."""
        if self.stop_event.is_set():
            return
        for name in ("tx-worker", "rx-reader"):
            thread = self._threads.get(name)
            if thread is not None and not thread.is_alive():
                log.error("Thread %s stopped unexpectedly; restarting it", name)
                self._spawn(name)

    def stop(self):
        self.stop_event.set()
        with self._radio_lock:
            self._pending_acks.clear()
            self._radio_changed.notify_all()

    def submit(self, item):
        self.queue.put(item)

    def _ui(self, *event):
        if self._on_ui:
            self._on_ui(event)

    def _submit_web(self, origin, session, seq, records):
        if self.web:
            self.web.submit(origin, session, seq, records)

    def _next_seq(self):
        self._seq = self._seq % SEQ_MAX + 1
        return self._seq

    def _worker(self):
        carry = None
        while not self.stop_event.is_set():
            if carry is None:
                try:
                    carry = self.queue.get(timeout=0.2)
                except queue.Empty:
                    continue
            batch, carry = self._collect_batch(carry)
            try:
                self._transmit(batch)
            except Exception:
                log.exception("Unexpected error while transmitting")
                self._ui("tx_failed")
            finally:
                for _ in batch:
                    self.queue.task_done()

    def _collect_batch(self, first):
        """Merges cues for different zones that arrive within COALESCE_WINDOW_SEC into one frame."""
        batch = [first]
        if first.kind != "cue":
            return batch, None
        zones = first.zones()
        deadline = time.monotonic() + COALESCE_WINDOW_SEC
        while zones != {"1", "2"}:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                nxt = self.queue.get(timeout=remaining)
            except queue.Empty:
                break
            merged = [r for item in batch + [nxt] for r in item.records]
            if nxt.kind == "cue" and not (zones & nxt.zones()) and frame_fits(merged):
                batch.append(nxt)
                zones |= nxt.zones()
            else:
                return batch, nxt
        return batch, None

    def _transmit(self, batch):
        if not self._begin_transmission():
            return
        try:
            self._transmit_batch(batch)
        finally:
            with self._radio_lock:
                self._transmitting = False
                self._radio_changed.notify_all()

    def _begin_transmission(self):
        with self._radio_changed:
            while not self.stop_event.is_set():
                delay = max(self._channel_free_at, self._remote_free_at,
                            self._radio_busy_until) - time.monotonic()
                if delay <= 0 and not self._pending_acks and not self._transmitting:
                    self._transmitting = True
                    return True
                self._radio_changed.wait(timeout=min(0.2, delay) if delay > 0 else 0.05)
        return False

    def _transmit_batch(self, batch):
        kind = batch[0].kind
        records = [rec for item in batch for rec in item.records]
        bursts = TEST_BURSTS if kind == "test" else DEFAULT_BURSTS
        seq = self._next_seq()
        if kind == "reload":
            label = "RELOAD"
        elif kind == "osc":
            label = f"OSC {batch[0].osc!r}"
        else:
            label = describe_records(records)
        if records:
            self._ui("records", records)

        if not self.link.ensure_open():
            self.failures += 1
            self._ui("tx_failed", seq)
            log.error("NOT SENT Seq #%d (no LoRa dongle connected): %s", seq, label)
            if kind == "cue" and records:
                self._submit_web(self.station_id, self.session, seq, records)
            return

        previous_seq = self.tracker.prepare_window(seq, label)
        sent, airtime, frame_len = 0, 0.0, 0
        try:
            for burst in range(1, bursts + 1):
                if self.stop_event.is_set():
                    break
                if kind == "reload":
                    frame = encode_reload(self.station_id, self.session, seq, burst, bursts)
                elif kind == "osc":
                    frame = encode_osc(self.station_id, self.session, seq, burst, bursts, batch[0].osc)
                else:
                    frame = encode_cue(self.station_id, self.session, seq, burst, bursts, records)
                if len(frame) > MAX_FRAME_BYTES:
                    raise ProtocolError("command exceeds maximum frame size")
                if not self.link.write(frame):
                    break
                if not sent:
                    self.osc_standby.clear_forwarded()
                sent += 1
                frame_len = len(frame)
                airtime += lora_toa_s(frame_len)
                with self._radio_lock:
                    self._radio_busy_until = time.monotonic() + lora_toa_s(frame_len)
                if burst < bursts:
                    self.stop_event.wait(burst_gap_s(frame_len))
        except Exception:
            self.tracker.cancel_window(seq, previous_seq)
            raise

        if not sent:
            self.tracker.cancel_window(seq, previous_seq)
            self.failures += 1
            self._ui("tx_failed", seq)
            log.error("NOT SENT Seq #%d (serial write failed): %s", seq, label)
            if kind == "cue" and records:
                self._submit_web(self.station_id, self.session, seq, records)
            return

        last_write = time.monotonic()
        toa = lora_toa_s(frame_len)
        ack_deadline = last_write + ack_window_s(frame_len, self.tracker.slot_count())
        with self._radio_lock:
            self._channel_free_at = ack_deadline

        self._rx_at_send[seq] = (self.link.rx_bytes, self._rx_rejected)
        while len(self._rx_at_send) > 64:
            del self._rx_at_send[next(iter(self._rx_at_send))]
        self.tracker.open_window(seq, label, ack_deadline, last_write, toa)
        if sent < bursts:
            log.warning("PARTIAL Seq #%d: only %d/%d bursts written: %s", seq, sent, bursts, label)
        else:
            log.info("Sent Seq #%d (%dx burst, %.0f ms air each): %s", seq, bursts, toa * 1000, label)

        self._report_duty(airtime, toa)
        self._ui("sent_seq", seq, sent == bursts)
        if kind == "cue" and records:
            self._submit_web(self.station_id, self.session, seq, records)

    def _report_duty(self, airtime, toa):
        with self._radio_lock:
            pct, hourly = self._duty.add(airtime)
        self._ui("duty", pct, hourly, toa * 1000)
        if pct >= DUTY_CYCLE_LIMIT_PCT:
            level = logging.ERROR
        elif pct >= 0.8 * DUTY_CYCLE_LIMIT_PCT:
            level = logging.WARNING
        else:
            level = logging.INFO
        log.log(level, "  -> Duty cycle (last hour): %.2f%% (%.1fs of %.0fs allowed)",
                pct, hourly, 36.0 * DUTY_CYCLE_LIMIT_PCT)

    def _reader(self):
        splitter = LineSplitter()
        dedup = Deduplicator()
        failures = 0
        while not self.stop_event.is_set():
            try:
                self._reader_step(splitter, dedup)
            except Exception:
                failures += 1
                log.exception("Receive loop error (%d in a row); continuing", failures)
                splitter.reset()
                if failures >= 3:
                    self.link.reconnect()
                self.stop_event.wait(min(5.0, 0.2 * failures))
            else:
                failures = 0

    def _reader_step(self, splitter, dedup):
        self._service_preview_acks()
        if not self.link.connected:
            if not self.link.ensure_open():
                self.stop_event.wait(0.5)
                return
            splitter.reset()
        for line, rssi in splitter.feed(self.link.read_available(
                timeout=self._preview_read_timeout())):
            try:
                self._handle_line(line, dedup, rssi)
            except Exception:
                log.exception("Error handling received line %r", line[:80])
        self._service_preview_acks()

    def _preview_read_timeout(self):
        with self._radio_lock:
            timeout = self.link.read_timeout
            if self._pending_acks and not self._transmitting:
                due = max(min(self._pending_acks.values()), self._radio_busy_until)
                timeout = min(timeout, due - time.monotonic())
            return max(0.0, timeout)

    def _schedule_preview_ack(self, frame, frame_len, is_new):
        if not self.preview_enabled or self.stop_event.is_set():
            return
        now = time.monotonic()
        with self._radio_lock:
            if self.stop_event.is_set():
                return
            if is_new:
                self._recent_remote.append((frame.session, frame.seq, now))
                sessions = {session for session, _, _ in self._recent_remote}
                self._pending_acks = {session: due for session, due in self._pending_acks.items()
                                      if session in sessions}
            elif frame.session not in self._pending_acks:
                return
            due = now + ack_delay_s(frame_len, frame.burst, frame.bursts, self.ack_slot)
            self._pending_acks[frame.session] = max(self._pending_acks.get(frame.session, due), due)
            slots = max(self.tracker.slot_count(), self.ack_slot + 1)
            remote_end = now + ack_delay_s(frame_len, frame.burst, frame.bursts, 0)
            remote_end += slots * ACK_SLOT_S + ACK_WINDOW_MARGIN_S
            self._remote_free_at = max(self._remote_free_at, remote_end)
            self._radio_changed.notify_all()

    def _service_preview_acks(self):
        with self._radio_lock:
            now = time.monotonic()
            if self._transmitting or now < self._radio_busy_until:
                return
            for session, due in list(self._pending_acks.items()):
                if now < due or self.stop_event.is_set():
                    continue
                del self._pending_acks[session]
                seqs = [seq for source, seq, seen in self._recent_remote
                        if source == session and now - seen <= ACK_HISTORY_S][-ACK_MAX_SEQS:]
                if not seqs:
                    self._radio_changed.notify_all()
                    continue
                if now - due > ACK_SLOT_S:
                    log.warning("[ACK] Preview missed its slot for session %s by %.0f ms",
                                session, (now - due) * 1000)
                frame = encode_ack(self.station_id, self.ack_slot, session, seqs)
                if self.link.write(frame):
                    toa = lora_toa_s(len(frame))
                    self._radio_busy_until = time.monotonic() + toa
                    self._report_duty(toa, toa)
                    log.info("[ACK] Preview %s written for session %s Seq %s (slot %d; RF delivery unconfirmed)",
                             self.station_id, session, ",".join(map(str, seqs)), self.ack_slot)
                    self._radio_changed.notify_all()
                    break
                log.warning("[ACK] Preview write failed for session %s", session)
                self._radio_changed.notify_all()

    def _handle_line(self, line, dedup, rssi=None):
        if b"~" not in line:
            self._rx_rejected += 1
            log.debug("Ignoring non-LoRaQ serial line: %r", line[:80])
            return
        try:
            frame = decode_frame(line)
        except ProtocolError as e:
            self._rx_rejected += 1
            log.warning("RX rejected (%s): %r", e, line[:80])
            return
        frame.rssi = rssi
        self.osc_standby.clear_forwarded()
        if frame.kind == "A":
            self.tracker.on_ack(frame.station, frame.session, frame.acked, rssi, frame.slot,
                                len(line) - line.rfind(b"~") + 2)
        elif frame.kind == "O":
            self.tracker.on_online(frame.station, frame.version, frame.slot, rssi)
        elif not (frame.station == self.station_id and frame.session == self.session):
            is_new = dedup.is_new((frame.station, frame.session, frame.seq))
            self._schedule_preview_ack(frame, len(line) - line.rfind(b"~") + 2, is_new)
            if not is_new:
                return
            if frame.session == self.session:
                log.warning("Transmitter %s shares this session ID; ACKs may be ambiguous until a restart",
                            frame.station)
            self._ui("received_source", frame.station, frame.rssi)
            log.info("Remote cue from another transmitter %s (session %s): %s%s",
                     frame.station, frame.session, describe_frame(frame), rssi_suffix(rssi))
            if frame.kind == "Q":
                self.osc_standby.handle(frame, rssi)
                return
            if frame.records:
                if self.web and SETTINGS.web_push.push_remote and not is_test_frame(frame):
                    self._submit_web(frame.station, frame.session, frame.seq, frame.records)
                self._ui("remote_records", frame.records)
            self._ui("remote_cue", frame.station, frame.session, frame.seq)

    def _monitor(self):
        while not self.stop_event.wait(0.1):
            try:
                self._monitor_once()
            except Exception:
                log.exception("ACK monitor error")
                self.stop_event.wait(1.0)

    def _monitor_once(self):
        self._check_rx_health(self.tracker.poll())
        now = time.monotonic()
        if now - self._last_supervised >= SUPERVISE_INTERVAL_S:
            self._last_supervised = now
            self._supervise()

    def _check_rx_health(self, closed):
        """Reopens the dongle if it returns nothing at all, not even garbled data, for several cue windows."""
        for window in closed:
            at_send = self._rx_at_send.pop(window.seq, None)
            if at_send is None or not window.expected:
                continue
            bytes_heard = self.link.rx_bytes - at_send[0]
            rejected = self._rx_rejected - at_send[1]
            missing = sorted(window.expected - window.acked)
            if missing:
                hint = ("nothing at all was received" if bytes_heard == 0 else
                        "corrupted frames were heard (collision or interference)" if rejected else
                        "other traffic was heard but no ACK from them")
                log.info("Seq #%d ACK window: %d byte(s) received, %d unparseable frame(s); %s; missing: %s",
                         window.seq, bytes_heard, rejected, hint, ", ".join(missing))
            if window.acked or bytes_heard:
                self._silent_windows = 0
            else:
                self._silent_windows += 1
        if self._silent_windows < RX_SILENT_WINDOWS:
            return
        now = time.monotonic()
        if self._last_rx_recovery is not None and now - self._last_rx_recovery < RX_RECOVERY_INTERVAL_S:
            return
        with self._radio_lock:
            if self._transmitting:
                return
            self._last_rx_recovery = now
            log.error("RX FAULT: no LoRa data of any kind received during the last %d ACK windows; "
                      "reopening the dongle connection. If this repeats, check or replug the dongle.",
                      self._silent_windows)
            self._silent_windows = 0
            self.link.reconnect()


class RangeTest:
    """Sends configured test pings; ACK summaries show link health per station."""

    def __init__(self, tx):
        self._tx = tx
        self._stop = threading.Event()
        self._stop.set()

    @property
    def active(self):
        return not self._stop.is_set()

    def toggle(self):
        if self.active:
            self.stop()
        else:
            self.start()

    def start(self):
        if self.active:
            return
        self._stop.clear()
        threading.Thread(target=self._run, name="range-test", daemon=True).start()
        log.info("Range test started: one ping every %.1f seconds. Type 'test' to stop.", TEST_INTERVAL_S)

    def stop(self):
        if self.active:
            self._stop.set()
            log.info("Range test stopped")

    def _run(self):
        count = 0
        while not self._stop.is_set() and not self._tx.stop_event.is_set():
            started = time.monotonic()
            count += 1
            colour = COLOUR_CYCLE[(count - 1) % len(COLOUR_CYCLE)]
            text = f"TEST #{count}\n{time.strftime('%H:%M:%S')}"
            self._tx.submit(TxItem("test", [Record("D", "1", colour, None, 0, text)]))
            self._tx.queue.join()
            self._stop.wait(max(0.0, TEST_INTERVAL_S - (time.monotonic() - started)))


def submit_cue(tx, test, args, source):
    try:
        item = build_item(args)
    except CueError as e:
        log.error("%s cue rejected: %s", source, e)
        return
    test.stop()
    tx.submit(item)


# ----------------------------------------------------------------------
# Interactive console
# ----------------------------------------------------------------------
def print_banner(tx, preview_enabled):
    print("==================================================")
    print(f" LoRaQ Transmitter v{APP_VERSION} | station {tx.station_id} (session {tx.session})")
    print(f" Hardware: {tx.link.port or 'NOT CONNECTED (will keep retrying)'}")
    print(f" Listening for OSC on {OSC_IP}:{OSC_PORT}, addresses /display, /clear, /alert, /oscsend, /oscstandby")
    forward = f"{OSC_FORWARD_IP}:{OSC_FORWARD_PORT} (configure with /oscstandby <OSC command>)" if OSC_FORWARD_IP else "Disabled"
    print(f" OSC Forwarding: {forward}")
    print(f" Preview Engine: {'Enabled (Resizable Window)' if preview_enabled else 'Disabled (Headless)'}")
    if tx.preview_enabled:
        print(f" Preview ACKs: {tx.station_id} (slot {tx.ack_slot})")
    else:
        print(" Preview ACKs: Disabled")


def print_help():
    print(HELP_TEXT)


def console_loop(tx, test):
    while not tx.stop_event.is_set():
        console_handler.prompt_active = True
        try:
            user_input = input(PROMPT).strip()
        except (EOFError, KeyboardInterrupt):
            break
        finally:
            console_handler.prompt_active = False
        if not user_input:
            continue

        cmd = user_input.lower()
        if cmd in ("help", "?", "h"):
            print_help()
            continue
        if cmd in ("quit", "exit"):
            break
        if cmd in ("test", "ping", "test on", "test start"):
            test.toggle()
            continue
        if cmd in ("test off", "test stop"):
            test.stop()
            continue
        if cmd == "oscstandby" or cmd.startswith("oscstandby "):
            try:
                standby_args = shlex.split(user_input)[1:]
                if standby_args == ["0"]:
                    tx.osc_standby.disarm("Console")
                else:
                    tx.osc_standby.arm("Console", _osc_command(standby_args))
            except (ValueError, CueError) as e:
                log.error("Standby command rejected: %s", e)
            continue
        if cmd == "standby" or cmd.startswith("standby "):
            log.error("Use 'oscstandby <OSC command>' or 'oscstandby 0'")
            continue
        if cmd == "osc" or cmd.startswith("osc "):
            log.error("Use 'oscsend' with no parameters to trigger the configured OSC command")
            continue

        if cmd.startswith("/display"):
            user_input = user_input[len("/display"):].strip()
        if "|" in user_input and not cmd.startswith(("osc ", "oscsend ")):
            args = [user_input]
        else:
            try:
                args = shlex.split(user_input)
            except ValueError as e:
                log.error("Syntax error (unclosed quote?): %s", e)
                continue
        submit_cue(tx, test, args, "Console")
    tx.stop()


# ----------------------------------------------------------------------
# Run modes
# ----------------------------------------------------------------------
def osc_port_in_use():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        try:
            probe.bind((OSC_IP, OSC_PORT))
        except OSError:
            return True
    return False


def run_one_shot(args):
    if args and args[0].startswith("/display"):
        args = args[1:]
    elif args and args[0] == "/clear":
        args = ["clear", *args[1:]]
    try:
        item = build_item(args)
    except CueError as e:
        log.error("Cue rejected: %s", e)
        return 2

    if osc_port_in_use():
        op = item.records[0].op if item.records else None
        if op == "F":
            address, forwarded = "/alert", [item.records[0].effect_id]
        elif op == "C":
            area = item.records[0].area
            address, forwarded = "/clear", [] if area == "0" else [int(area)]
        elif item.kind == "osc":
            address, forwarded = "/oscsend", []
        else:
            address, forwarded = "/display", list(args)
        udp_client.SimpleUDPClient(OSC_IP, OSC_PORT).send_message(address, forwarded)
        log.info("Forwarded to the running transmitter via OSC %s:%d", OSC_IP, OSC_PORT)
        return 0

    link = SerialLink(BAUD_RATE)
    if not link.ensure_open():
        log.error("Cue NOT sent: no LoRa dongle available")
        return 1
    tx = Transmitter(link, web=create_pusher(SETTINGS.web_push, SETTINGS.station.station_id, APP_VERSION))
    tx.start()
    tx.submit(item)
    tx.queue.join()
    tx.tracker.wait_closed(timeout=max(3.0, tx._channel_free_at - time.monotonic() + 0.5))
    tx.stop()
    if tx.web:
        tx.web.stop(timeout=5.0)
    link.close()
    return 1 if tx.failures else 0


def on_osc_standby(tx, args):
    if len(args) == 1 and _arg_str(args[0]).lower() in ("0", "off", "false", "no"):
        tx.osc_standby.disarm("OSC")
    else:
        try:
            tx.osc_standby.arm("OSC", _osc_command(args))
        except CueError as e:
            log.error("OSC standby command rejected: %s", e)


def service_ui_events(engine, ui_queue, audio=None):
    redraw = False
    while True:
        try:
            kind, *payload = ui_queue.get_nowait()
        except queue.Empty:
            break
        if kind in ("records", "remote_records"):
            if engine is not None:
                engine.apply_records(*payload)
                redraw = True
            if kind == "remote_records" and audio is not None:
                audio.play_records(*payload)
        elif engine is not None:
            redraw = True
            if kind == "duty":
                engine.update_duty_cycle(*payload)
            elif kind == "stations":
                engine.set_stations(*payload)
            elif kind == "sent_seq":
                engine.set_sent_seq(*payload)
            elif kind == "tx_failed":
                engine.set_tx_failed(*payload)
            elif kind == "web_status":
                engine.set_web_status(*payload)
            elif kind == "remote_cue":
                engine.set_remote_cue(*payload)
            elif kind == "received_source":
                engine.set_received_source(*payload)
            elif kind == "osc_status":
                engine.set_osc_status(*payload)
    return redraw


def run_preview_loop(tx, engine, ui_queue, audio=None):
    clock = pygame.time.Clock()
    redraw = True
    while not tx.stop_event.is_set():
        if engine.set_link_status(tx.link.connected, tx.link.last_error):
            redraw = True
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                tx.stop()
            elif event.type == pygame.VIDEORESIZE:
                screen = pygame.display.set_mode((max(400, event.w), max(320, event.h)), pygame.RESIZABLE)
                engine.resize(screen)
                redraw = True
            elif event.type == pygame.VIDEOEXPOSE:
                redraw = True

        if service_ui_events(engine, ui_queue, audio):
            redraw = True

        if redraw or engine.has_active_effects(time.monotonic()):
            engine.render()
            pygame.display.flip()
            redraw = False
        clock.tick(60)


def run_interactive():
    if osc_port_in_use():
        log.error("OSC port %s:%d is already in use (is another LoRaQ transmitter running?)", OSC_IP, OSC_PORT)
        return 1

    engine = None
    ui_queue = queue.Queue()
    if GFX_AVAILABLE:
        try:
            pygame.display.init()
            pygame.font.init()
            set_app_icon("LoRaQ.Transmitter")
            pygame.display.set_caption("LoRaQ Transmitter - Display Preview")
            screen = pygame.display.set_mode((PREVIEW_WIDTH, PREVIEW_HEIGHT + PREVIEW_STATS_HEIGHT), pygame.RESIZABLE)
            engine = LoRaQEngine(screen, show_telemetry=True, station_id=SETTINGS.station.station_id,
                                 ack_slot=SETTINGS.station.ack_slot,
                                 show_web_info=SETTINGS.web_push.enabled)
        except pygame.error as e:
            log.warning("Graphics init failed (%s); running headless", e)
            engine = None
    else:
        log.info("Pygame or loraq_gfx_engine.py not available; running headless")

    link = SerialLink(BAUD_RATE)
    link.ensure_open()
    if engine:
        engine.set_link_status(link.connected, link.last_error)
    def report_web_status(*status):
        ui_queue.put(("web_status", *status))

    tx = Transmitter(link, on_ui=ui_queue.put, preview_enabled=engine is not None,
                     web=create_pusher(SETTINGS.web_push, SETTINGS.station.station_id, APP_VERSION,
                                       on_status=report_web_status))
    test = RangeTest(tx)

    disp = dispatcher.Dispatcher()
    disp.map("/display", lambda address, *args: submit_cue(tx, test, args, "OSC"))
    disp.map("/clear", lambda address, *args: submit_cue(tx, test, ["clear", *args], "OSC"))
    disp.map("/alert", lambda address, *args: submit_cue(tx, test, ["alert", *args], "OSC"))
    disp.map("/oscsend", lambda address, *args: submit_cue(tx, test, ["oscsend", *args], "OSC"))
    disp.map("/oscstandby", lambda address, *args: on_osc_standby(tx, args))
    try:
        server = osc_server.ThreadingOSCUDPServer((OSC_IP, OSC_PORT), disp)
    except OSError as e:
        log.error("Could not open OSC port %s:%d: %s", OSC_IP, OSC_PORT, e)
        return 1
    audio = CueAudio(SETTINGS.transmitter.audio_enabled) if CueAudio is not None else None
    if audio is None and SETTINGS.transmitter.audio_enabled:
        log.warning("Remote cue audio unavailable; install pygame and loraq_audio.py on this transmitter")
    threading.Thread(target=server.serve_forever, name="osc", daemon=True).start()

    tx.start()
    print_banner(tx, engine is not None)
    print_help()
    threading.Thread(target=console_loop, args=(tx, test), name="console", daemon=True).start()

    try:
        if engine:
            run_preview_loop(tx, engine, ui_queue, audio)
        else:
            while not tx.stop_event.wait(0.02):
                service_ui_events(None, ui_queue, audio)
    except KeyboardInterrupt:
        pass
    finally:
        print()
        log.info("Shutting down OSC server and serial port...")
        test.stop()
        tx.stop()
        if tx.web:
            tx.web.stop()
        server.shutdown()
        server.server_close()
        link.close()
        if audio is not None:
            audio.close()
        if engine or audio is not None:
            pygame.quit()
    return 0


def main():
    argv = sys.argv[1:]
    debug = "--debug" in argv
    argv = [a for a in argv if a != "--debug"]
    setup_logging(debug)
    log.info("Config %s | SF%d BW%g kHz CR 4/%d | %d cue bursts | %d ms ACK slots",
             SETTINGS.path, SETTINGS.radio.spreading_factor, SETTINGS.radio.bandwidth_khz,
             SETTINGS.radio.coding_rate + 4, DEFAULT_BURSTS, round(SETTINGS.timing.ack_slot_s * 1000))
    log.info("Link: %s interface | port %s", SETTINGS.link.interface,
             find_lora_port() or f"not found (available: {available_ports_hint()})")

    if argv and argv[0].lower() in ("--config", "-c", "config", "setup"):
        log.error("Radio configuration is a separate operation; use the hardware-specific example in configsamples/")
        return 2
    if argv and argv[0].lower() in ("--help", "-h"):
        print(__doc__)
        print_help()
        return 0
    if argv:
        return run_one_shot(argv)
    return run_interactive()


if __name__ == "__main__":
    sys.exit(main())
