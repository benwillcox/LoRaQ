"""LoRaQ v2 wire protocol, radio timing and serial transport shared by the transmitter and receivers.

Frame layout (one line per LoRa packet):  ~BODY*CRC\\r\\n
    Cue:     C|STATION|SESSION|SEQ|BURST/BURSTS|RECORD[^RECORD]
    Reload:  R|STATION|SESSION|SEQ|BURST/BURSTS|
    OSC:     Q|STATION|SESSION|SEQ|BURST/BURSTS|ESCAPED_OSC_COMMAND
  Ack:     A|STATION|SLOT|SESSION|SEQ[,SEQ...]
  Online:  O|STATION|VERSION|SLOT
    Record:  OP|AREA|COLOUR|TIMER_SECONDS|EFFECT_ID|TEXT   (OP: D=display, C=clear, F=effect)
"""
from __future__ import annotations

import binascii
import logging
import os
import random
import re
import shlex
import threading
import time
from dataclasses import dataclass, field

import serial
import serial.tools.list_ports

from loraq_config import SETTINGS, lora_airtime_s

APP_VERSION = "1.1"

log = logging.getLogger("loraq")

LORA_SF = SETTINGS.radio.spreading_factor
LORA_BW_KHZ = SETTINGS.radio.bandwidth_khz
LORA_CR = SETTINGS.radio.coding_rate
LORA_PREAMBLE = SETTINGS.radio.preamble_symbols

BURST_MARGIN_S = SETTINGS.timing.burst_margin_s
BURST_JITTER_S = SETTINGS.timing.burst_jitter_s
ACK_GUARD_S = SETTINGS.timing.ack_guard_s
ACK_SLOT_S = SETTINGS.timing.ack_slot_s
ACK_WINDOW_MARGIN_S = SETTINGS.timing.ack_window_margin_s
ACK_HISTORY_S = SETTINGS.timing.ack_history_s
ACK_MAX_SEQS = SETTINGS.timing.ack_max_sequences
MAX_SLOT = 31
MAX_FRAME_BYTES = SETTINGS.radio.max_frame_bytes
MAX_TIMER_SECONDS = 99 * 60 + 59
SEQ_MAX = 9999

COLOUR_LETTERS = ("W", "R", "G", "Y", "B", "O")
EFFECTS = {
    0: (False, None, "none"),
    1: (True, None, "flash"),
    2: (True, "buzzer", "flash + buzzer"),
    3: (False, "buzzer", "buzzer"),
    4: (True, "beep", "flash + beep"),
    5: (False, "beep", "beep"),
    6: (True, "double-beep", "flash + double-beep"),
    7: (False, "double-beep", "double-beep"),
}

USB_SYMLINK = "/dev/lora_dongle"
# Raspberry Pi GPIO header UART exposed by a LoRa HAT, in preference order.
HAT_PORTS = ("/dev/serial0", "/dev/ttyAMA0", "/dev/ttyS0")
_USB_KEYWORDS = ("usbserial", "usbmodem", "cp210", "ch34", "ftdi", "ft232", "waveshare", "ttyacm",
                 "ttyusb", "usb-serial", "usb serial", "silicon labs", "prolific")

_ESCAPE_TABLE = str.maketrans({"%": "%25", "|": "%7C", "^": "%5E", "~": "%7E", "*": "%2A", "\n": "%0A"})
_UNESCAPES = {"%25": "%", "%7C": "|", "%5E": "^", "%7E": "~", "%2A": "*", "%0A": "\n"}
_UNESCAPE_RE = re.compile("%(?:25|7C|5E|7E|2A|0A)")
_STATION_RE = re.compile(r"[A-Za-z0-9_-]{1,16}", re.ASCII)
_SESSION_RE = re.compile(r"[0-9A-F]{4}", re.ASCII)
_TIMER_RE = re.compile(r"(?:(\d{1,2}):)?(\d{1,4})", re.ASCII)
_INT_RE = re.compile(r"\d{1,4}", re.ASCII)
_OSC_INT_RE = re.compile(r"[+-]?\d{1,9}", re.ASCII)
_OSC_FLOAT_RE = re.compile(r"[+-]?(?:\d+\.\d*|\.\d+)(?:[eE][+-]?\d+)?", re.ASCII)


class ProtocolError(ValueError):
    pass


@dataclass
class Record:
    op: str
    area: str
    colour: str = "W"
    timer: int | None = None
    effect_id: int = 0
    text: str = ""

    def zones(self) -> set[str]:
        return {"1", "2"} if self.area == "0" else {self.area}

    def encode(self) -> str:
        if type(self.effect_id) is not int or self.effect_id not in EFFECTS:
            raise ProtocolError(f"bad effect ID {self.effect_id!r}")
        timer = "" if self.timer is None else str(self.timer)
        return "|".join([self.op, self.area, self.colour, timer, str(self.effect_id), escape_text(self.text)])


@dataclass
class Frame:
    kind: str
    session: str = ""
    seq: int = 0
    burst: int = 1
    bursts: int = 1
    records: list[Record] = field(default_factory=list)
    station: str = ""
    version: str = ""
    slot: int = 0
    acked: list[int] = field(default_factory=list)
    osc: str = ""
    rssi: int | None = None


# ----------------------------------------------------------------------
# Text, timer and identifier helpers
# ----------------------------------------------------------------------
def escape_text(text: str) -> str:
    cleaned = "".join(c for c in text if c == "\n" or (ord(c) >= 32 and c != "\x7f"))
    return cleaned.translate(_ESCAPE_TABLE)


def unescape_text(text: str) -> str:
    return _UNESCAPE_RE.sub(lambda m: _UNESCAPES[m.group(0)], text)


def parse_timer(value: str) -> int:
    """Parses 'MM:SS' or plain seconds into total seconds."""
    match = _TIMER_RE.fullmatch(value.strip())
    if not match or (match.group(1) is not None and int(match.group(2)) > 59):
        raise ValueError(f"Invalid timer '{value}' (use MM:SS or seconds)")
    total = int(match.group(1) or 0) * 60 + int(match.group(2))
    if total > MAX_TIMER_SECONDS:
        raise ValueError(f"Timer '{value}' is longer than 99:59")
    return total


def format_timer(seconds: int) -> str:
    minutes, secs = divmod(seconds, 60)
    return f"{minutes:02d}:{secs:02d}"


def valid_station_id(station: str) -> bool:
    return bool(_STATION_RE.fullmatch(station))


def describe_records(records: list[Record]) -> str:
    parts = []
    for rec in records:
        if rec.op == "F":
            parts.append(f"EFFECT {rec.effect_id}: {EFFECTS[rec.effect_id][2]}")
            continue
        if rec.op == "C":
            parts.append("CLEAR all" if rec.area == "0" else f"CLEAR Z{rec.area}")
            continue
        bits = [f"Z{rec.area}", repr(rec.text.replace("\n", "\\n")), rec.colour]
        if rec.timer is not None:
            bits.append(format_timer(rec.timer))
        if rec.effect_id:
            bits.append(EFFECTS[rec.effect_id][2])
        parts.append(" ".join(bits))
    return " + ".join(parts)


def describe_frame(frame: Frame) -> str:
    if frame.kind == "Q":
        return f"OSC {frame.osc!r}"
    return "RELOAD" if frame.kind == "R" else describe_records(frame.records)


def rssi_dbm(rssi: int | None) -> int | None:
    return None if rssi is None else rssi - 256 if rssi >= 128 else rssi


def rssi_suffix(rssi: int | None) -> str:
    if rssi is None:
        return ""
    # REG3's appended byte is not documented; observed values match signed dBm.
    return f" [RSSI: {rssi_dbm(rssi):.1f} dBm]"


def parse_osc_command(text: str) -> tuple[str, list[int | float | str]]:
    """Splits '/address arg ...' into an OSC address and int/float/str arguments."""
    if any(ord(c) < 32 or c == "\x7f" for c in text):
        raise ValueError("OSC command contains control characters")
    try:
        parts = shlex.split(text)
    except ValueError as e:
        raise ValueError(f"OSC command syntax error: {e}") from None
    if not parts or not parts[0].startswith("/") or len(parts[0]) < 2:
        raise ValueError("OSC command must start with an address such as /cue/go")
    if any(c in parts[0] for c in " #"):
        raise ValueError(f"Invalid OSC address {parts[0]!r}")
    args: list[int | float | str] = []
    for part in parts[1:]:
        if _OSC_INT_RE.fullmatch(part):
            args.append(int(part))
        elif _OSC_FLOAT_RE.fullmatch(part):
            args.append(float(part))
        else:
            args.append(part)
    return parts[0], args


# ----------------------------------------------------------------------
# Encoding
# ----------------------------------------------------------------------
def crc16(body: str) -> str:
    return f"{binascii.crc_hqx(body.encode('utf-8'), 0xFFFF):04X}"


def _wrap(body: str) -> bytes:
    return f"~{body}*{crc16(body)}\r\n".encode("utf-8")


def encode_cue(station: str, session: str, seq: int, burst: int, bursts: int, records: list[Record]) -> bytes:
    _check_station(station)
    payload = "^".join(rec.encode() for rec in records)
    return _wrap(f"C|{station}|{session}|{seq}|{burst}/{bursts}|{payload}")


def encode_reload(station: str, session: str, seq: int, burst: int, bursts: int) -> bytes:
    _check_station(station)
    return _wrap(f"R|{station}|{session}|{seq}|{burst}/{bursts}|")


def encode_osc(station: str, session: str, seq: int, burst: int, bursts: int, command: str) -> bytes:
    _check_station(station)
    return _wrap(f"Q|{station}|{session}|{seq}|{burst}/{bursts}|{escape_text(command)}")


def encode_ack(station: str, slot: int, session: str, seqs: list[int]) -> bytes:
    return _wrap(f"A|{station}|{slot}|{session}|{','.join(str(s) for s in seqs)}")


def encode_online(station: str, version: str, slot: int) -> bytes:
    return _wrap(f"O|{station}|{version}|{slot}")


def frame_fits(records: list[Record]) -> bool:
    return len(encode_cue(SETTINGS.station.station_id, "FFFF", SEQ_MAX, 9, 9, records)) <= MAX_FRAME_BYTES


def osc_fits(command: str) -> bool:
    return len(encode_osc(SETTINGS.station.station_id, "FFFF", SEQ_MAX, 9, 9, command)) <= MAX_FRAME_BYTES


# ----------------------------------------------------------------------
# Decoding & validation
# ----------------------------------------------------------------------
def _parse_int(value: str, lo: int, hi: int, what: str) -> int:
    if not _INT_RE.fullmatch(value) or not lo <= int(value) <= hi:
        raise ProtocolError(f"bad {what} {value!r}")
    return int(value)


def _check_session(session: str) -> None:
    if not _SESSION_RE.fullmatch(session):
        raise ProtocolError(f"bad session id {session!r}")


def _check_station(station: str) -> None:
    if not valid_station_id(station):
        raise ProtocolError(f"bad station id {station!r}")


def _decode_record(raw: str) -> Record:
    op, area, colour, timer_s, effect_s, text = raw.split("|", 5)
    if op == "D":
        if area not in ("1", "2"):
            raise ProtocolError(f"bad display area {area!r}")
    elif op == "C":
        if area not in ("0", "1", "2"):
            raise ProtocolError(f"bad clear area {area!r}")
    elif op == "F":
        if area != "0" or colour != "W" or timer_s or text:
            raise ProtocolError("effect-only record must not contain display fields")
    else:
        raise ProtocolError(f"bad record op {op!r}")
    if colour not in COLOUR_LETTERS:
        raise ProtocolError(f"bad colour {colour!r}")
    timer = _parse_int(timer_s, 0, MAX_TIMER_SECONDS, "timer") if timer_s else None
    if effect_s not in {str(effect_id) for effect_id in EFFECTS}:
        raise ProtocolError(f"bad effect ID {effect_s!r}")
    effect_id = int(effect_s)
    if op == "C" and effect_id:
        raise ProtocolError("clear record must not request an effect")
    text = unescape_text(text)
    if any(ord(c) < 32 and c != "\n" for c in text):
        raise ProtocolError("control characters in text")
    return Record(op, area, colour, timer, effect_id, text)


def _decode_cue(body: str) -> Frame:
    kind, station, session, seq_s, burst_s, payload = body.split("|", 5)
    _check_station(station)
    _check_session(session)
    seq = _parse_int(seq_s, 1, SEQ_MAX, "sequence")
    burst_k, burst_n = burst_s.split("/")
    burst = _parse_int(burst_k, 1, 9, "burst")
    bursts = _parse_int(burst_n, burst, 9, "burst count")
    records = []
    osc = ""
    if kind == "C":
        records = [_decode_record(raw) for raw in payload.split("^")]
    elif kind == "Q":
        osc = unescape_text(payload)
        if osc and (not osc.startswith("/") or any(ord(c) < 32 for c in osc)):
            raise ProtocolError("bad OSC command")
    elif payload:
        raise ProtocolError("unexpected payload on RELOAD")
    return Frame(kind, session=session, seq=seq, burst=burst, bursts=bursts,
                 records=records, osc=osc, station=station)


def _decode_ack(body: str) -> Frame:
    _, station, slot_s, session, seqs_s = body.split("|")
    _check_station(station)
    slot = _parse_int(slot_s, 0, MAX_SLOT, "slot")
    _check_session(session)
    seqs = [_parse_int(s, 1, SEQ_MAX, "sequence") for s in seqs_s.split(",")]
    if len(seqs) > ACK_MAX_SEQS:
        raise ProtocolError("too many sequences in ACK")
    return Frame("A", session=session, station=station, slot=slot, acked=seqs)


def _decode_online(body: str) -> Frame:
    _, station, version, slot_s = body.split("|")
    _check_station(station)
    if not version or len(version) > 12:
        raise ProtocolError(f"bad version {version!r}")
    return Frame("O", station=station, version=version, slot=_parse_int(slot_s, 0, MAX_SLOT, "slot"))


_DECODERS = {"C": _decode_cue, "R": _decode_cue, "Q": _decode_cue, "A": _decode_ack, "O": _decode_online}


def decode_frame(line: bytes) -> Frame:
    """Decodes one received line; raises ProtocolError on corruption or malformed content."""
    start = line.rfind(b"~")
    if start < 0:
        raise ProtocolError("no frame marker")
    try:
        text = line[start + 1:].decode("utf-8").strip()
    except UnicodeDecodeError:
        raise ProtocolError("invalid UTF-8") from None
    body, sep, crc = text.rpartition("*")
    if not sep:
        raise ProtocolError("missing CRC")
    if crc != crc16(body):
        raise ProtocolError("CRC mismatch")
    kind = body[:1]
    decoder = _DECODERS.get(kind)
    if decoder is None:
        raise ProtocolError(f"unknown frame type {kind!r}")
    try:
        return decoder(body)
    except ProtocolError:
        raise
    except ValueError:
        raise ProtocolError(f"malformed '{kind}' frame") from None


# ----------------------------------------------------------------------
# Radio timing (Semtech LoRa time-on-air)
# ----------------------------------------------------------------------
def lora_toa_s(payload_bytes: int) -> float:
    return lora_airtime_s(payload_bytes, SETTINGS.radio)


_max_ack = encode_ack("S" * 16, MAX_SLOT, "FFFF", [SEQ_MAX] * ACK_MAX_SEQS)
if len(_max_ack) > MAX_FRAME_BYTES:
    raise SystemExit("[CONFIG ERROR] Maximum ACK exceeds [radio] max_frame_bytes")
if ACK_SLOT_S <= lora_toa_s(len(_max_ack)) + 0.02:
    raise SystemExit("[CONFIG ERROR] Derived ACK slot is shorter than the maximum ACK airtime")


def burst_gap_s(frame_len: int, worst_case: bool = False) -> float:
    jitter = BURST_JITTER_S if worst_case else random.uniform(0, BURST_JITTER_S)
    return lora_toa_s(frame_len) + BURST_MARGIN_S + jitter


def ack_delay_s(frame_len: int, burst: int, bursts: int, slot: int) -> float:
    """Receiver delay after hearing a burst before its ACK slot opens."""
    return (bursts - burst) * burst_gap_s(frame_len, worst_case=True) + ACK_GUARD_S + slot * ACK_SLOT_S


def ack_window_s(frame_len: int, slot_count: int) -> float:
    """Transmitter wait after writing the final burst before judging ACKs."""
    return lora_toa_s(frame_len) + ACK_GUARD_S + slot_count * ACK_SLOT_S + ACK_WINDOW_MARGIN_S


# ----------------------------------------------------------------------
# Serial transport
# ----------------------------------------------------------------------
def available_ports_hint() -> str:
    """Lists serial ports the OS can enumerate, for 'device not found' messages."""
    try:
        ports = sorted(f"{p.device} ({p.description})" for p in serial.tools.list_ports.comports())
    except Exception as e:
        return f"unavailable ({e})"
    return ", ".join(ports) if ports else "none enumerated"


def _list_ports() -> list:
    # Windows enumeration can fail transiently while a USB serial device re-enumerates.
    try:
        return list(serial.tools.list_ports.comports())
    except Exception as e:
        log.debug("Serial port enumeration failed: %s", e)
        return []


def find_lora_port(interface: str | None = None) -> str | None:
    """Port priority: $LORAQ_PORT, [link] port, then auto-detection for the selected interface."""
    interface = interface or SETTINGS.link.interface
    env_port = os.environ.get("LORAQ_PORT")
    if env_port:
        return env_port
    if SETTINGS.link.port:
        return SETTINGS.link.port
    if interface in ("auto", "usb"):
        if os.path.exists(USB_SYMLINK):
            return USB_SYMLINK
        for port in _list_ports():
            info = f"{port.device} {port.description} {port.hwid}".lower()
            if any(key in info for key in _USB_KEYWORDS):
                return port.device
    if interface in ("auto", "hat"):
        for candidate in HAT_PORTS:
            if os.path.exists(candidate):
                return candidate
    return None


class SerialLink:
    """Connection to the LoRa dongle that reopens itself after an unplug or error."""

    RETRY_INTERVAL_S = 2.0
    RECONNECT_LOCK_TIMEOUT_S = 2.0

    def __init__(self, baud: int, port: str | None = None, read_timeout: float = 0.05,
                 interface: str | None = None):
        self.baud = baud
        self.fixed_port = port
        self.read_timeout = read_timeout
        self.interface = interface or SETTINGS.link.interface
        self.port: str | None = None
        self.io_lock = threading.RLock()
        self._ser: serial.Serial | None = None
        self._lock = threading.Lock()
        self._next_attempt = 0.0
        self._warned = False
        self.last_error: str | None = "Serial device not initialised"
        self.rx_bytes = 0
        self.last_rx: float | None = None

    @property
    def connected(self) -> bool:
        ser = self._ser
        return ser is not None and ser.is_open

    def ensure_open(self) -> bool:
        with self._lock:
            if self.connected:
                return True
            now = time.monotonic()
            if now < self._next_attempt:
                return False
            self._next_attempt = now + self.RETRY_INTERVAL_S
            ser = None
            port = None
            try:
                port = self.fixed_port or find_lora_port(self.interface)
                if not port:
                    raise serial.SerialException(
                        f"no '{self.interface}' LoRa device found; set [link] port. Ports: {available_ports_hint()}")
                # exclusive: a second LoRaQ instance would otherwise silently split the RX stream
                ser = serial.Serial(port, self.baud, timeout=self.read_timeout, write_timeout=1.0,
                                    exclusive=True)
                ser.reset_input_buffer()
            except Exception as e:
                self.last_error = str(e)
                if ser is not None:
                    try:
                        ser.close()
                    except Exception:
                        pass
                if not self._warned:
                    log.warning("LoRa device unavailable (%s); retrying every %.0fs", e, self.RETRY_INTERVAL_S)
                    self._warned = True
                return False
            self._ser, self.port, self._warned = ser, port, False
            self.last_error = None
        log.info("Connected to LoRa dongle on %s", port)
        return True

    def write(self, data: bytes) -> bool:
        with self.io_lock:
            ser = self._ser
            if ser is None:
                return False
            try:
                written = ser.write(data)
                if written != len(data):
                    raise serial.SerialException(f"short write ({written}/{len(data)} bytes)")
                ser.flush()
                return True
            except (serial.SerialException, OSError) as e:
                self._drop(e)
                return False

    def read_available(self, timeout: float | None = None) -> bytes:
        with self.io_lock:
            ser = self._ser
            if ser is None:
                return b""
            try:
                read_timeout = self.read_timeout if timeout is None else timeout
                if ser.timeout != read_timeout:
                    ser.timeout = read_timeout
                data = ser.read(ser.in_waiting or 1)
            except (serial.SerialException, OSError) as e:
                self._drop(e)
                return b""
            if data:
                self.rx_bytes += len(data)
                self.last_rx = time.monotonic()
            return data

    def reconnect(self) -> bool:
        """Closes the port so the next ensure_open() reopens it immediately.

        Returns False if serial I/O was stuck; the port is then closed without the I/O lock
        so that a blocked read or write is aborted.
        """
        got_lock = self.io_lock.acquire(timeout=self.RECONNECT_LOCK_TIMEOUT_S)
        if not got_lock:
            log.error("Serial I/O on %s is stuck; forcing the connection closed", self.port)
        try:
            self.close()
            with self._lock:
                self._next_attempt = 0.0
        finally:
            if got_lock:
                self.io_lock.release()
        return got_lock

    def close(self) -> None:
        with self._lock:
            ser, self._ser = self._ser, None
        if ser is not None:
            try:
                ser.close()
            except (serial.SerialException, OSError):
                pass

    def _drop(self, err: Exception) -> None:
        with self._lock:
            ser, self._ser = self._ser, None
            self.last_error = str(err)
        if ser is not None:
            log.error("Serial link to %s lost: %s", self.port, err)
            try:
                ser.close()
            except (serial.SerialException, OSError):
                pass


class LineSplitter:
    """Accumulates raw serial bytes and yields complete lines."""

    MAX_BUFFER = 1024

    def __init__(self):
        self._buf = bytearray()
        self._pending_line: bytes | None = None

    def reset(self) -> None:
        self._buf.clear()
        self._pending_line = None

    def feed(self, chunk: bytes) -> list[tuple[bytes, int]]:
        self._buf += chunk
        lines = []
        while True:
            if self._pending_line is None:
                idx = self._buf.find(b"\n")
                if idx < 0:
                    break
                self._pending_line = bytes(self._buf[:idx]).strip()
                del self._buf[:idx + 1]
                if not self._pending_line:
                    self._pending_line = None
                    continue
            if not self._buf:
                break
            rssi = self._buf.pop(0)
            lines.append((self._pending_line, rssi))
            self._pending_line = None
        if len(self._buf) > self.MAX_BUFFER:
            log.warning("Discarding %d bytes of unterminated serial data", len(self._buf))
            self._buf.clear()
        return lines


class Deduplicator:
    """Remembers command keys for a time window so burst copies are applied once."""

    def __init__(self, window_s: float = 120.0):
        self.window_s = window_s
        self._seen: dict[tuple, float] = {}

    def is_new(self, key: tuple) -> bool:
        now = time.monotonic()
        if len(self._seen) > 512:
            self._seen = {k: t for k, t in self._seen.items() if now - t < self.window_s}
        seen_at = self._seen.get(key)
        if seen_at is not None and now - seen_at < self.window_s:
            return False
        self._seen[key] = now
        return True
