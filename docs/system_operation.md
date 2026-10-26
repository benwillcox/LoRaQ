
### 1. Integration with show control software - QLab, SCS etc (OSC Receiver Mode)
`LoRaQ-transmitter` acts as an OSC-to-LoRa gateway.

It opens UDP port `53070` (configurable in `loraq.ini`) listening for OSC messages targeted at `/display`, `/clear`, `/alert`, `/oscstandby` and `/oscsend`.  

**Setting up a Network Cue in QLab:**
1. **Network Patch:** Set destination IP to `127.0.0.1` (if running LoRaQ-transmitter on the same machine) and port to `53070` (matching the previously set port).
2. **OSC Type:** Select `Raw OSC message`.
3. **OSC Command Examples:**
   * `/display "STANDBY CUE 12" 1 yellow`
   * `/display "GO SCENE 2" 1 green 01:30 2`
   * `/display "CUE 42 ACTIVE" 2 white 00:45`
   * `/clear 2`
   * `/alert 6`

`/alert <id>` takes exactly one integer alert ID and broadcasts the selected alert to all receivers and listening transmitters. Both zones retain their displayed text, colours and running timers. Listening transmitters play the sound and mirror the visual alert when a preview is open; their own outgoing cues remain silent. See further below for all possible values of <id>.

---

### 2. One-Shot Mode (Shell Scripting & Automation)
`LoRaQ-transmitter` can be executed directly as a command-line tool within bash scripts or automated event workflows if your application does not support OSC. If an interactive transmitter is already running, the one-shot cue is forwarded to it over OSC. This avoids a serial port conflict and lets back-to-back one-shots share a session. Otherwise it opens the LoRa device directly, waits for ACKs and prints the summary. Exit codes: `0` sent, `1` not sent, `2` invalid cue.

**Note:** Arguments are parsed positionally. If you wish to provide an Alert ID (`0-7`), you must also explicitly provide the `[Area]` and `[Colour]` to avoid ambiguity in parsing.

```bash
# General One-Shot Syntax:
LoRaQ-transmitter "<Text>" [Area] [Colour] [Timer: MM:SS] [Alert: 0-7]

# Examples:
LoRaQ-transmitter "BEGIN ACT 1" 1 green 02:00
LoRaQ-transmitter "STAGE CLEAR" 2 red
LoRaQ-transmitter clear 1
LoRaQ-transmitter "GO" 1 green 00:30 2
LoRaQ-transmitter /alert 3

# Pipe Syntax One-Shot:
LoRaQ-transmitter "SPEED CUE|1|R|00:30|1"
```

---

### 3. Interactive Console Commands
When running `LoRaQ-transmitter` interactively, operators can trigger commands directly via the prompt:

```bash
# Console Syntax Examples:
LoRaQ Tx > "STANDBY SCENE 1" 1 red
LoRaQ Tx > "INTERVAL" 1 yellow 15:00
LoRaQ Tx > "STAGE READY" 2 green
LoRaQ Tx > "GO" 1 green 00:30 2
LoRaQ Tx > alert 6     # Also accepts /alert 6; leaves the displayed cue intact

# Clear Commands:
LoRaQ Tx > clear 1      # Clears Area 1 only
LoRaQ Tx > clear 2      # Clears Area 2 (slides panel down)
LoRaQ Tx > clear        # Clears both areas

# Diagnostics & Maintenance:
LoRaQ Tx > test         # Toggles continuous 3-second ping test
LoRaQ Tx > reload       # Forces remote display script restart (after each receiver ACKs)
LoRaQ Tx > help         # Re-displays the command syntax
LoRaQ Tx > exit         # Cleanly shuts down transmitter & releases OSC port
```

In positional syntax, a bare integer after the area/colour selects an alert ID (`0`-`7`). Timers require `MM:SS` with two second digits, for example `00:30`. Give an explicit area and colour when specifying an alert to avoid ambiguity with optional area `1`/`2`.

### 4. ACK Monitoring Output
```text
10:42:01 INFO    Sent Seq #42 (3x burst, 108 ms air each): Z1 'GO SCENE 2' G + Z2 'CUE 42' W
10:42:02 INFO    Seq #42 ACK: CAB_1 OK, STAGE_LEFT MISSING
10:42:02 ERROR   ALERT: STAGE_LEFT NO ACK for 1 consecutive cues (delivery unconfirmed; check power / RF)
```
The first missing ACK raises an ALERT when the acknowledgement window closes. The preview shows acknowledged stations as green (OK) and stations missing an ACK as red (NO RESPONSE). A later ACK confirming a newer cue returns the station to OK.

A missing ACK means delivery is unconfirmed; the station may have received the cue but its reply was lost. Check power, radio reception and unique ACK slots. LoRaQ does not automatically re-send cues in response to missing ACKs.

### 5. OSC Relay (Transmitter to Transmitter)

One LoRaQ transmitter can send a trigger via LoRa to another remote LoRaQ transmitter, to tell it to send an OSC command to its host show control software. 

Imagine the following scenario as an example:

* Station A needs to wait at a certain point in the show for a remote LoRa cue from Station B before it continues to the next cue.

Station A - Machine running QLab and LoRaQ-transmitter. 
When the show gets to a position where it needs to wait for a remote trigger, QLab on station A sends a specified OSC string to its local LoRaQ-transmitter using `/oscstandby`, e.g:

`/oscstandby /workspace/<workspace_id>/go <cue_number>`

This tells the local LoRaQ to wait for a trigger via LoRa before acting on the cue, and only then will it send that cue to the QLab instance.

When the cue needs to be fired, Station B (also running QLab and LoRaQ-transmitter) sends `/oscsend` via OSC to its LoRaQ-transmitter. This is broadcast over LoRa which is received by Machine A, which immediately releases the standing-by OSC string to QLab.

1. On Station A, set `osc_forward_ip` and `osc_forward_port` in `[transmitter]`. These are the destination host IP address and UDP port that a standing-by cue will be sent to (usually the same QLab machine i.e. 127.0.0.1 and the default Qlab listen port is 53000).
2. On Station A arm it by sending `/oscstandby /workspace/<workspace_id>/go <cue_number>` to the LoRaQ-transmitter OSC listen interface, as per any other LoRaQ cue - you will see OSC STANDBY in the transmitter preview.
3. On Station B, send `/oscsend` to the LoRaQ-transmitter OSC listen interface, as per any other LoRaQ cue.
4. Station A will then send the standing-by OSC command, shows `OSC FORWARDED` in green and disarms. Any oscsend commands heard while not in standby are shown as `OSC IGNORED` in red.

You can disarm before the oscsend is received with `/oscstandby 0`.

---

### Alert IDs

| ID | Screen | Sound |
| :--- | :--- | :--- |
| `0` | No new alert | None |
| `1` | Flash | None |
| `2` | Flash | Buzzer |
| `3` | Unchanged | Buzzer |
| `4` | Flash | Beep |
| `5` | Unchanged | Beep |
| `6` | Flash | Double-beep |
| `7` | Unchanged | Double-beep |

Flashes retain the two-second duration and maximum three flashes per second. The buzzer is a 0.6-second low tone; the beep is a 0.2-second high tone; double-beep plays two short high tones with a gap. Sound starts once when a new cue is applied.