import math
import sys
import time
from pathlib import Path

import serial

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from loraq_config import SETTINGS  # noqa: E402
from loraq_protocol import available_ports_hint, find_lora_port  # noqa: E402

CONFIG_BAUD = 9600  # The HAT always talks at 9600 baud in configuration mode (M0 low, M1 high).

# Register 0x03 bits 7-5: UART baud rate code.
UART_CODES = {1200: 0, 2400: 1, 4800: 2, 9600: 3, 19200: 4, 38400: 5, 57600: 6, 115200: 7}
# Register 0x03 bits 2-0: air data rate code (kbps).
AIR_RATES = {0.3: 0, 1.2: 1, 2.4: 2, 4.8: 3, 9.6: 4, 19.2: 5, 38.4: 6, 62.5: 7}
# Register 0x04 bits 1-0: transmit power code (dBm).
POWER_CODES = {22: 0, 17: 1, 13: 2, 10: 3}
HAT_MAX_PACKET = 240


def build_registers(radio):
    if radio.baud_rate not in UART_CODES:
        raise SystemExit(f"[radio] baud_rate {radio.baud_rate} is not supported by the HAT "
                         f"(use one of {', '.join(map(str, UART_CODES))})")

    # The HAT has no SF/BW/CR settings, so pick the air rate closest to the configured LoRa bit rate.
    lora_kbps = (radio.spreading_factor * radio.bandwidth_khz / 2 ** radio.spreading_factor
                 * 4 / (4 + radio.coding_rate))
    air_kbps = min(AIR_RATES, key=lambda rate: abs(math.log(rate / lora_kbps)))
    power_dbm = min(POWER_CODES, key=lambda dbm: abs(dbm - radio.power))

    reg0 = (UART_CODES[radio.baud_rate] << 5) | AIR_RATES[air_kbps]  # 8N1 parity bits left at 00
    reg1 = POWER_CODES[power_dbm]  # 240-byte sub-packets, ambient noise RSSI off
    reg3 = 0x80  # Transparent mode, RSSI byte enabled, no relay or LBT
    registers = bytes([
        0x00, 0x00,  # Module address 0
        radio.network_id,
        reg0,
        reg1,
        radio.channel,
        reg3,
        radio.key >> 8, radio.key & 0xFF,
    ])

    print(f"Config: {SETTINGS.path}")
    print(f"  UART {radio.baud_rate} baud, NetID {radio.network_id}, channel {radio.channel}, key {radio.key}")
    print(f"  Air rate {air_kbps:g} kbps (closest to SF{radio.spreading_factor}/BW{radio.bandwidth_khz:g}/"
          f"CR 4/{radio.coding_rate + 4} = {lora_kbps:.2f} kbps)")
    print(f"  Power {power_dbm} dBm (closest HAT step to {radio.power} dBm)")
    if radio.max_frame_bytes > HAT_MAX_PACKET:
        print(f"WARNING: max_frame_bytes {radio.max_frame_bytes} exceeds the HAT's {HAT_MAX_PACKET}-byte packet; "
              "frames will be split over the air.")
    return registers


def main():
    registers = build_registers(SETTINGS.radio)
    write_all_cmd = b"\xC0\x00" + bytes([len(registers)]) + registers

    port = find_lora_port()
    if not port:
        print(f"ERROR: No '{SETTINGS.link.interface}' LoRa adapter found; "
              f"set [link] port in loraq.ini. Ports: {available_ports_hint()}")
        return 1
    print("Port:", port)

    ser = serial.Serial(port, CONFIG_BAUD, timeout=1)
    try:
        print("Writing full configuration to adapter:", write_all_cmd.hex(" ").upper())
        ser.write(write_all_cmd)
        time.sleep(0.5)
        response = ser.read_all()
    finally:
        ser.close()

    if not response:
        print("ERROR: No response from the adapter. It is probably not in configuration mode -")
        print("       HAT: set M0 low and M1 high; EByte dongle: hold the config button")
        print("       for about 3 seconds until the LED turns red, then run this again.")
        return 1
    print("Adapter Write Response (Hex):", response.hex(" ").upper())
    if response != b"\xC1" + write_all_cmd[1:]:
        print("WARNING: Response does not echo the written settings; the configuration may not have been saved.")
        return 1
    print("Adapter configured successfully.")
    return 0


def run():
    try:
        return main()
    except (serial.SerialException, OSError) as error:
        print(f"ERROR: Could not configure the adapter: {error}")
        print("Stop any transmitter or display using the port, and check the adapter connection.")
        return 1
    except KeyboardInterrupt:
        print("\nAdapter configuration cancelled.")
        return 1
    finally:
        # macOS app bundles already keep their Terminal session open in the launcher.
        if sys.platform == "win32" and getattr(sys, "frozen", False) and sys.stdin.isatty():
            try:
                input("\nPress Return to close this window.")
            except (EOFError, KeyboardInterrupt):
                print("\nConsole session closed.")


if __name__ == "__main__":
    sys.exit(run())
