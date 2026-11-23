"""Read and validate the shared LoRaQ INI configuration."""
from __future__ import annotations

import configparser
import math
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RadioSettings:
    baud_rate: int
    channel: int
    power: int
    network_id: int
    key: int
    spreading_factor: int
    bandwidth_khz: float
    coding_rate: int
    preamble_symbols: int
    max_frame_bytes: int


@dataclass(frozen=True)
class LinkSettings:
    interface: str
    port: str | None


@dataclass(frozen=True)
class TimingSettings:
    burst_margin_s: float
    burst_jitter_s: float
    ack_guard_s: float
    ack_slot_s: float
    ack_window_margin_s: float
    ack_history_s: float
    ack_max_sequences: int


@dataclass(frozen=True)
class TransmitterSettings:
    osc_ip: str
    osc_port: int
    retries: int
    test_retries: int
    test_interval_s: float
    coalesce_window_s: float
    min_ack_slots: int
    duty_cycle_limit_pct: float
    max_dwell_time: float
    preview_width: int
    preview_height: int
    osc_forward_ip: str | None = None
    osc_forward_port: int | None = None
    audio_enabled: bool = True


@dataclass(frozen=True)
class StationSettings:
    station_id: str
    ack_slot: int
    diagnostic_log: bool = False


@dataclass(frozen=True)
class ReceiverSettings:
    target_fps: int
    audio_enabled: bool = False
    show_tx_info: bool = True


@dataclass(frozen=True)
class WebPushSettings:
    enabled: bool = False
    url: str | None = None
    token: str | None = None
    push_remote: bool = True
    timeout_s: float = 5.0
    max_queue: int = 500
    heartbeat_s: float = 30.0


@dataclass(frozen=True)
class Settings:
    radio: RadioSettings
    link: LinkSettings
    timing: TimingSettings
    transmitter: TransmitterSettings
    receiver: ReceiverSettings
    station: StationSettings
    expected_stations: dict[str, int]
    path: Path
    web_push: WebPushSettings = WebPushSettings()


def lora_airtime_s(payload_bytes: int, radio: RadioSettings) -> float:
    symbol_s = (2 ** radio.spreading_factor) / (radio.bandwidth_khz * 1000.0)
    low_data_rate = 1 if symbol_s >= 0.016 else 0
    bits = 8 * payload_bytes - 4 * radio.spreading_factor + 28 + 16
    symbols = 8 + max(0, math.ceil(bits / (4.0 * (radio.spreading_factor - 2 * low_data_rate)))
                       * (radio.coding_rate + 4))
    return (radio.preamble_symbols + 4.25 + symbols) * symbol_s


def default_config_path() -> Path:
    if getattr(sys, "frozen", False):
        executable = Path(sys.executable).resolve()
        if sys.platform == "darwin":
            for parent in executable.parents:
                if parent.name == "Contents" and parent.parent.suffix == ".app":
                    return parent.parent.parent / "loraq.ini"
        return executable.with_name("loraq.ini")
    return Path(__file__).with_name("loraq.ini")


def load_settings(path: str | Path | None = None) -> Settings:
    location = Path(path or os.environ.get("LORAQ_CONFIG") or default_config_path()).expanduser()
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    try:
        if not parser.read(location, encoding="utf-8"):
            raise ValueError(f"Configuration file not found: {location}")

        def number(section, name, minimum, maximum, cast=int):
            value = cast(parser.get(section, name))
            if not minimum <= value <= maximum:
                raise ValueError(f"[{section}] {name} must be between {minimum} and {maximum}")
            return value

        radio = RadioSettings(
            number("radio", "baud_rate", 1200, 1000000),
            number("radio", "channel", 0, 255),
            number("radio", "power", 0, 30),
            number("radio", "network_id", 0, 255),
            number("radio", "key", 0, 65535),
            number("radio", "spreading_factor", 7, 12),
            number("radio", "bandwidth_khz", 62.5, 500, float),
            number("radio", "coding_rate", 1, 4),
            number("radio", "preamble_symbols", 6, 64),
            number("radio", "max_frame_bytes", 32, 255),
        )
        if radio.bandwidth_khz not in (62.5, 125.0, 250.0, 500.0):
            raise ValueError("[radio] bandwidth_khz must be 62.5, 125, 250 or 500")

        # Older configs predate [link]; auto-detection keeps them working unchanged.
        interface = parser.get("link", "interface", fallback="auto").strip().lower()
        if interface not in ("auto", "usb", "hat"):
            raise ValueError("[link] interface must be auto, usb or hat")
        link_port = parser.get("link", "port", fallback="").strip()
        if link_port and not re.fullmatch(r"[A-Za-z0-9_.:/\\-]{1,64}", link_port):
            raise ValueError("[link] port must be a device name such as /dev/serial0 or COM4")
        link = LinkSettings(interface=interface, port=link_port or None)

        ack_max_sequences = 4
        max_ack_size = len(f"~A|{'S' * 16}|31|FFFF|{','.join(['9999'] * ack_max_sequences)}*FFFF\r\n")
        if max_ack_size > radio.max_frame_bytes:
            raise ValueError("[radio] max_frame_bytes must fit the maximum ACK frame")
        ack_slot_s = math.ceil((lora_airtime_s(max_ack_size, radio) + 0.04) * 100) / 100
        timing = TimingSettings(
            burst_margin_s=0.04,
            burst_jitter_s=0.04,
            ack_guard_s=max(0.15, 0.25 * lora_airtime_s(radio.max_frame_bytes, radio)),
            ack_slot_s=ack_slot_s,
            ack_window_margin_s=0.40,
            ack_history_s=max(5.0, 32 * ack_slot_s + 1.0),
            ack_max_sequences=ack_max_sequences,
        )
        forward_ip = parser.get("transmitter", "osc_forward_ip", fallback="").strip()
        forward_port_s = parser.get("transmitter", "osc_forward_port", fallback="").strip()
        forward_port = number("transmitter", "osc_forward_port", 1, 65535) if forward_port_s else None
        if bool(forward_ip) != bool(forward_port):
            raise ValueError("[transmitter] osc_forward_ip and osc_forward_port must both be set or both blank")
        if forward_ip and not re.fullmatch(r"[A-Za-z0-9_.:-]{1,253}", forward_ip):
            raise ValueError("[transmitter] osc_forward_ip must be an IP address or host name")
        transmitter = TransmitterSettings(
            osc_ip=parser.get("transmitter", "osc_ip").strip(),
            osc_port=number("transmitter", "osc_port", 1, 65535),
            retries=number("transmitter", "retries", 0, 8),
            test_retries=0,
            test_interval_s=3.0,
            coalesce_window_s=0.06,
            min_ack_slots=4,
            duty_cycle_limit_pct=number("transmitter", "duty_cycle_limit_percent", 0.01, 100, float),
            max_dwell_time=float(parser.get("transmitter", "max_dwell_time", fallback="0")),
            preview_width=(number("transmitter", "preview_width", 400, 7680)
                           if parser.has_option("transmitter", "preview_width") else 640),
            preview_height=(number("transmitter", "preview_height", 320, 4320)
                            if parser.has_option("transmitter", "preview_height") else 360),
            osc_forward_ip=forward_ip or None,
            osc_forward_port=forward_port,
            audio_enabled=parser.getboolean("transmitter", "audio_enabled", fallback=True),
        )
        if not 0 <= transmitter.max_dwell_time < float("inf"):
            raise ValueError("[transmitter] max_dwell_time must be a non-negative finite number of milliseconds")
        station_id = parser.get("station", "station_id").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,16}", station_id):
            raise ValueError("[station] station_id must be 1-16 letters, digits, _ or -")
        slot_text = parser.get("station", "ack_slot").strip()
        if not re.fullmatch(r"\d{1,2}", slot_text, re.ASCII) or int(slot_text) > 31:
            raise ValueError("[station] ack_slot must be an explicit integer from 0 to 31")
        station = StationSettings(station_id, int(slot_text),
                                  parser.getboolean("station", "diagnostic_log", fallback=False))
        receiver = ReceiverSettings(
            target_fps=30,
            audio_enabled=parser.getboolean("receiver", "audio_enabled", fallback=False),
            show_tx_info=parser.getboolean("receiver", "show_tx_info", fallback=True),
        )
        expected = {}
        for station_id, value in parser.items("expected_stations"):
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,16}", station_id):
                raise ValueError(f"Invalid expected station name {station_id!r}")
            slot = int(value)
            if not 0 <= slot <= 31:
                raise ValueError(f"[expected_stations] {station_id} slot must be 0-31")
            expected[station_id] = slot
        slots = list(expected.values())
        if len(slots) != len(set(slots)):
            raise ValueError("[expected_stations] each station needs a unique ACK slot")

        web_push = WebPushSettings()
        if parser.has_section("web_push"):
            url = parser.get("web_push", "url", fallback="").strip().rstrip("/")
            token = parser.get("web_push", "token", fallback="").strip()
            if url and not re.fullmatch(r"https?://[^\s\"'<>]+", url, re.IGNORECASE):
                raise ValueError("[web_push] url must start with https:// (or http:// for local testing)")
            if token and not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", token, re.ASCII):
                raise ValueError("[web_push] token must be 16-128 letters, digits, _ or -")
            web_push = WebPushSettings(
                enabled=parser.getboolean("web_push", "enabled", fallback=False),
                url=url or None,
                token=token or None,
                push_remote=parser.getboolean("web_push", "push_remote", fallback=True),
                timeout_s=(number("web_push", "timeout_s", 1, 60, float)
                           if parser.get("web_push", "timeout_s", fallback="").strip() else 5.0),
                max_queue=(number("web_push", "max_queue", 10, 10000)
                           if parser.get("web_push", "max_queue", fallback="").strip() else 500),
                heartbeat_s=(number("web_push", "heartbeat_s", 5, 3600, float)
                             if parser.get("web_push", "heartbeat_s", fallback="").strip() else 30.0),
            )
            if web_push.enabled and not (web_push.url and web_push.token):
                raise ValueError("[web_push] url and token are required when enabled = true")

        return Settings(radio, link, timing, transmitter, receiver, station, expected, location, web_push)
    except (configparser.Error, ValueError) as exc:
        raise ValueError(f"{location}: {exc}") from exc


try:
    SETTINGS = load_settings()
except ValueError as exc:
    raise SystemExit(f"[CONFIG ERROR] {exc}") from None