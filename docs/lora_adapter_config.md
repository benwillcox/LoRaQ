### LoRa Adapter Configuration

LoRaQ is designed for use with the E22-900T22U USB dongle and Waveshare Pi Hat, both of which use an SX1262 LoRa transceiver. Please note that a Waveshare USB-TO-LoRa-xF type dongle cannot communicate with the Pi Hat and E22-900T22U, so it's best to avoid these, as they use a different onboard protocol/packet framing and will not link with other SX1262 hardware.

Both the EByte E22-900T22U USB dongle and Waveshare Pi Hat contain a non-volatile memory which must be programmed as a separate operation before starting LoRaQ.  

The radio settings in `[radio]` must match across every LoRa adapter in your LoRaQ system. These settings are defined in `loraq.ini` on each station; keep the defaults unless your hardware or local radio regulations require changes. Choose your own `key` value in the range 0–65535 and set it identically on all adapters, the modules use this communication key for encrypted radio transmission. 

`LoRaQ-adapter-configure` writes register hex codes to the Pi HAT and EByte dongle. 

First of all make sure all the `[radio]` parameters are set correctly in `loraq.ini`. These are used by the configuration script and also at runtime by `LoRaQ-transmitter` and `LoRaQ-display`.

`[link]` uses the same port detection as the transmitter and display. With `interface = auto` and `port = auto`, the utility looks for a recognised USB serial adapter first, then a Raspberry Pi GPIO UART. Use `interface = usb` or `interface = hat` to restrict detection to that connection type. If several serial adapters are connected, set `port` to the actual device port to avoid selecting the wrong adapter. Typical ports are Linux/Pi (`/dev/serial0`, `/dev/ttyAMA0` or `/dev/ttyUSB0`), macOS (`/dev/cu.usbserial-*`), and Windows (`COM3`, `COM4`, etc.). An explicit `[link] port` overrides detection.

Stop any running LoRaQ transmitter or display application before programming; only one process can use the serial port at a time.

To enter programming mode, for the Raspberry Pi hat keep M0 LOW and set M1 HIGH (remove the M1 jumper).

On the Ebyte dongle hold the config button for around 3 seconds then release, you will see the LED change to red to confirm programming mode.

Run:
```bash
LoRaQ-adapter-configure.exe (Windows)
OR
LoRaQ-adapter-configure.app (Mac)
OR
./LoRaQ-adapter-configure (Linux)
```
 to write the config to the adapter.

When configuration is complete move the M1 jumper back to the GND position on the Pi Hat, or hold the config button again for 3 seconds to return to normal mode on the Ebyte adapter.
Your LoRa adapter is now configured.

#### Checking your installation

Before using LoRaQ for a show, use range-test mode to check reception and signal strength at each operating position. Try the antennas in their intended positions, with any buildings, terrain or metalwork that will be between the devices during use. For moving installations, check the link along the route as well.

Enter test at the transmitter console to start sending periodic test messages. Check the acknowledgements and signal-strength readings, and confirm that each receiving display shows the changing test message. Enter test again to stop.

#### Choosing radio settings for your location

Check your local radio rules when choosing the frequency, transmit power and airtime settings. The permitted values vary by country and frequency band, so the supplied defaults may need adjusting. Keep the `[radio]` settings consistent across all your devices.

LoRaQ's estimated airtime statistics can help you assess how often you send cues, and show duty cycle as well as each transmission dwell. They are informational rather than an automatic limiter, so choose settings and cue intervals that stay within the limits for your location.
