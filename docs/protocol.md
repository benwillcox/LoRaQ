## Communication Protocol

Each LoRa packet is one line, which is assembled automatically from either the OSC message, command line parameter, or interactive console input, depending on the operating mode:

`~BODY*CRC\r\n`

| Frame | Body | Purpose |
| :--- | :--- | :--- |
| Cue | `C\|STATION\|SESSION\|SEQ\|BURST/BURSTS\|RECORD[^RECORD]` | Display / clear zones or trigger an alert |
| Reload | `R\|STATION\|SESSION\|SEQ\|BURST/BURSTS\|` | Restart receiver applications |
| OSC relay | `Q\|STATION\|SESSION\|SEQ\|BURST/BURSTS\|COMMAND` | Empty command triggers the configured OSC command on a standby transmitter |
| Ack | `A\|STATION\|SLOT\|SESSION\|SEQ[,SEQ...]` | Receiver confirms recent sequences; `SLOT` lets late-starting transmitters learn its ACK timing |
| Online | `O\|STATION\|VERSION\|SLOT` | Receiver boot announcement |

Each cue record is `OP|AREA|COLOUR|TIMER|ALERT_ID|TEXT`:

| Field | Description | Values |
| :--- | :--- | :--- |
| **OP** | Record type | `D` (display), `C` (clear), `F` (alert only) |
| **AREA** | Target zone | `1` (main), `2` (bottom panel); `0` = both (clear/alert only) |
| **COLOUR** | Text colour | `W`, `R`, `G`, `Y`, `B`, `O` |
| **TIMER** | Countdown in seconds | e.g. `90`, or empty for none |
| **ALERT_ID** | Flash/sound selection | integer `0` through `7` (see [Alert IDs](system_operation.md#alert-ids)) |
| **TEXT** | Cue text; `% \| ^ ~ *` and newlines are percent-escaped | any UTF-8 |

**Colours Key:** `W` = White, `R` = Red, `G` = Green, `Y` = Yellow, `B` = Blue, `O` = Orange.

`STATION` is the sender's mandatory `[station] station_id`: 1-16 ASCII letters, digits, `_` or `-`. It is included in every command burst and protected by the CRC. ACK and ONLINE packets already identify their sender. The ACK `SESSION` still refers to the transmitter session being acknowledged.

`SESSION` is a random 4-hex-digit ID chosen each time the transmitter starts, `SEQ` runs from 1 to 9999, and `CRC` is CRC-16/CCITT in hex. The frame layout, radio timing and serial transport all live in `loraq_protocol.py`. Packet-size limits and airtime calculations include the source-ID bytes.

RSSI is measured locally by the receiving radio and is not transmitted in the frame body. 

Alert-only records are `F|0|W||ID|`; they carry no text or timer and use the existing main-zone colour for a flash. Clear records always use alert ID `0`.

---

## Key Features
* **Blind Multi-Burst Transmission:** Every cue is sent 3 times (configurable). The gap between copies is calculated from the packet's airtime plus a margin and random jitter, so copies never overlap. Receivers apply the first good copy and log the rest as `[RX DUP]`.
* **CRC-Protected Framing:** Every packet has a start marker and a CRC-16. Corrupted, truncated or merged packets are rejected (`[RX REJECTED]`) instead of being displayed.
* **Session-Aware Deduplication:** A random session ID is chosen each time the transmitter starts, so restarts and one-shot commands are never mistaken for duplicates.
* **Zone Pairing:** Cues for zone 1 and zone 2 fired within 60 ms of each other (e.g. two back-to-back QLab network cues) are combined into one packet, so both zones update on the same frame.
* **ACK Monitoring:** Receivers and listening transmitters acknowledge commands in their own time slots. When an acknowledgement window closes, a missing ACK from a monitored station immediately raises an ALERT. This means delivery is unconfirmed, not necessarily that the station missed the command. Nothing is re-sent in response to a missing ACK; the configured redundant bursts are sent regardless. ACK slots run from 0 to 31 and must be unique across transmitters and receivers, allowing up to 32 stations per channel.
* **Auto-Reconnect:** Both transmitter and receiver reopen the serial port automatically if the dongle is unplugged.
* **Low-Overhead Receiver Radio Waits:** Idle serial reads block for up to 250 ms but return when data arrives. Pending ACK and ONLINE deadlines shorten the wait, reducing idle polling without intentionally delaying cues or acknowledgments. Transmitter read timing is unchanged.