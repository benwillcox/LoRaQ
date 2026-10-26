# LoRaQ - Long Range Cue Display

LoRaQ is an open-source cue display system for live theatre and event production. It sends cue text, countdown timers and visual/audio alerts from QLab, SCS or another OSC-capable show-control application to remote Raspberry Pi displays over LoRa radio.

The radio link does not require Wi-Fi, mobile coverage or an internet connection. The transmitter can run on the show-control computer or on another computer on the local network. An optional self-hosted web display lets crew follow cues in a browser where internet access is available.

![LoRaQ receiver showing two cue areas and independent countdown timers](images/receiver.png)

## How it works

1. **Show control:** Your show-control application sends OSC messages to the LoRaQ transmitter.
2. **Transmitter:** LoRaQ runs on a Mac, Windows or Linux computer and broadcasts cues through a supported USB LoRa radio adapter.
3. **Receiver displays:** Raspberry Pis with supported LoRa HATs receive the cues and show them on HDMI monitors.
4. **Acknowledgements:** Each receiving station replies over radio in its assigned acknowledgement slot. The transmitter previews the cue and shows ACK status and signal strength for the stations configured for monitoring.

If a monitored station has not replied by the end of its acknowledgement window, LoRaQ raises an alert. **A missing ACK means delivery is unconfirmed:** either the cue or its reply may have been lost. Missing ACKs do not trigger an automatic re-send.

![Transmitter statistics showing acknowledgements and signal strength](images/transmitter-stats.png)

## Features

- **Two cue areas:** A main display and a sliding lower panel, each with its own countdown timer. Text scales to fit; the lower panel slides away when cleared.
- **Text colours and alerts:** Selectable colour text, with screen flashes and optional buzzer, beep or double-beep alerts. Alerts can accompany a cue or be triggered without replacing the displayed text.
- **Transmitter preview and monitoring:** Cue preview, per-station ACK status and signal strength, plus estimated radio airtime and duty cycle.
- **OSC relay:** Arm an OSC command on a remote transmitter, then trigger it over LoRa to link show-control systems.
- **Range-test mode:** Periodic test messages for checking reception and signal strength at the intended operating positions.
- **Console and command-line operation:** Send cues interactively or from scripts as well as through OSC.

## Hardware requirements

LoRaQ is a software project, not a hardware kit. Hardware must be purchased and assembled separately.

| Position | Requirements |
| --- | --- |
| **Transmitter** | A Mac, Windows or Linux computer, with an EByte E22-900T22U USB LoRa adapter. Usually this is the computer already running show control, but it can be a separate computer receiving OSC over the local network. |
| **Each receiver** | A Raspberry Pi with a supported Waveshare SX1262 LoRa HAT, an HDMI monitor and suitable power. A Pi Zero running DietPi is the documented recommended setup |
| **Audio alerts** | Monitor speakers or another suitable audio output are required only if audible alerts are wanted. |

**Check radio compatibility before buying.** Adapters are not necessarily interoperable, even when they use the same radio chip. In particular, Waveshare USB-TO-LoRa-xF dongles cannot communicate with the supported EByte dongle and Pi HAT combination. See the [adapter configuration guide](docs/lora_adapter_config.md) for hardware and programming details.

## Getting started

1. **Obtain compatible hardware.** Plan the transmitter, receiver displays, antennas and power supplies using the setup guides below.
2. **Choose how to run LoRaQ.** Check [Releases](https://github.com/benwillcox/LoRaQ/releases) for available downloads, or follow the source installation instructions in the [transmitter](docs/transmitter_setup.md) and [receiver](docs/receiver_setup.md) guides. To build packaged applications yourself, see the [build instructions](packaging/README.md).
3. **Configure each station.** Edit its `loraq.ini`: assign a unique station ID and ACK slot, keep radio settings identical across the system, and configure the remote stations to monitor. Do not include the local transmitter in its own monitored-station list.
4. **Program the radio adapters.** Use the configuration utility before starting the transmitter or display. Changing the INI alone does not program the radio hardware. Follow the [adapter configuration guide](docs/lora_adapter_config.md).
5. **Start and test the system.** Run the transmitter and receiver applications, then use range-test mode to check reception and acknowledgements at the actual operating positions. For moving installations, test along the route as well.
6. **Connect show control.** Send OSC cues to the transmitter using the examples below and the [operations manual](docs/system_operation.md).

For source installations, keep the root-level Python scripts, shared modules, `assets/` folder and `loraq.ini` together. Packaged applications include the runtime assets; keep their external `loraq.ini` beside the executable or app bundle. Restart the application after changing its configuration.

## Quick OSC examples

By default, the transmitter listens on **`127.0.0.1:53070`**, accepting OSC only from the same computer. To receive OSC from another computer, change `osc_ip` in `loraq.ini` to the LoRaQ computer's local network IP address and allow incoming UDP traffic on the configured port through its firewall.

In QLab, use a Network cue with **Raw OSC message**:

| Action | OSC message |
| --- | --- |
| Main cue in Area 1 | `/display "STANDBY CUE 12" 1 yellow` |
| Cue with countdown and flash/buzzer alert | `/display "GO SCENE 2" 1 green 01:30 2` |
| Supplementary cue in Area 2 | `/display "CUE 42 ACTIVE" 2 white 00:45` |
| Flash and double-beep without replacing the cue | `/alert 6` |
| Clear Area 2 | `/clear 2` |
| Clear both areas | `/clear` |

Timers use `MM:SS`, for example `01:30`. When specifying an alert in a display cue, provide the area and colour explicitly.

See the [operations manual](docs/system_operation.md) for the full command syntax, colour and alert reference, console commands, command-line operation and two-stage OSC relay.

## Optional web display

The web display lets crew follow both cue areas, countdowns and alerts on a phone, tablet or computer using a private browser link.

- Host the supplied viewer yourself using **Apache, PHP and HTTPS**.
- Enable web push on at least one internet-connected LoRaQ transmitter or receiver.
- Viewers also need internet access; the radio link operates independently.
- Multiple devices can push updates for redundancy, with duplicate cues merged by the server.
- Browser audio alerts are optional and require user interaction to enable sound.

This is an additional way to view cues, not automatic failover for a missing radio acknowledgement. See the [web display setup guide](docs/web_push.md) for hosting, private links and device configuration.

## Documentation

| Guide | Contents |
| --- | --- |
| [Transmitter setup](docs/transmitter_setup.md) | Dependencies, station configuration, OSC networking and startup |
| [Receiver setup](docs/receiver_setup.md) | Raspberry Pi installation, HDMI/audio configuration and automatic startup |
| [Radio adapter configuration](docs/lora_adapter_config.md) | Compatible hardware, adapter programming and reception tests |
| [System operation and OSC commands](docs/system_operation.md) | Cue syntax, alerts, ACK monitoring, console operation and OSC relay |
| [Web display setup](docs/web_push.md) | Hosting the viewer and configuring web push |
| [Radio protocol reference](docs/protocol.md) | Frame format, cue records and acknowledgement behaviour |
| [Build instructions](packaging/README.md) | Building the transmitter, display and adapter configuration applications |

## Radio regulations

LoRa uses sub-GHz radio bands subject to local regulations. Permitted frequencies, transmit power, antenna gain, duty cycle and channel-access requirements depend on the country and the specific band in use. Check your regulator's current requirements before operating the system; the supplied settings are not a universal compliant configuration.

LoRaQ displays estimated duty cycle and frame airtime in the console and transmitter preview. The `duty_cycle_limit_percent` and `max_dwell_time` settings in `loraq.ini` control these indications only: **LoRaQ does not enforce regulatory limits or guarantee compliance.**

## Background and support

LoRaQ was originally developed by Ben Willcox for an immersive theatre production on a heritage railway. Transmitters on the station platform and train sent cues to displays across the site, including locomotive cabs, where countdown timers helped drivers synchronise movements with the performance.

For bugs and feature requests, [open an issue](https://github.com/benwillcox/LoRaQ/issues). Include the platform, hardware, relevant configuration and logs, removing private keys and tokens before sharing.

For a project overview, visit [loraq.co.uk](https://www.loraq.co.uk/).

## Licence and trademarks

LoRaQ is licensed under the **GNU General Public License v3.0 (GPLv3)**. See [LICENSE](LICENSE) for details.

*LoRa® is a registered trademark of Semtech Corporation. LoRaQ is an independent open-source project and is not affiliated with or endorsed by Semtech Corporation.*
