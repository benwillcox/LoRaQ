## Transmitter Setup & Requirements (`LoRaQ-transmitter`)

### Hardware requirements
The transmitter application will run on Mac, Windows or Linux, with an Ebyte E22-900T22U SX1262 based USB dongle to handle the LoRa transmissions. It would be considered normal practice to have this running on the same show control computer, for example QLab on Mac or SCS on Windows. However, it can also run on a completely separate machine and receive OSC control commands over the network from the show control machine.

Consider an external high-gain antenna depending on your range requirements and environment. The RSSI can be monitored using test mode to gauge the strength of your LoRa transmissions. Note: Keep the antenna positioned outside of metal enclosures or cabinets, as steel effectively blocks sub-GHz RF signals.

Multiple transmitters can operate in the same space, though simultaneous transmissions should be avoided to prevent RF packet collisions. It works well in situations where one show control computer is used for part of the show, then the show transitions to another area with a different show control computer.  

Each transmitter will receive commands sent from another transmitter and display this on the graphical preview in the same way as the display application, and will indicate this on the graphical preview and debug console along with the originating transmitter ID.

### Software Requirements

Download the appropriate release for your platform.

### Configuration (`loraq.ini`)
Edit `loraq.ini` beside the scripts on **each** machine, each field is documented within the .ini file.  

Set a unique `station_id` and `ack_slot` in `[station]` on every transmitter and receiver. In `[expected_stations]`, list the remote stations to monitor and their matching slots; do not include the transmitter's own station.

The default `osc_ip = 127.0.0.1` accepts OSC only from the same computer. If show control runs on another computer, set `osc_ip` to the LoRaQ computer's local network IP address and allow incoming UDP traffic on `osc_port` (default `53070`) through its firewall.

Write the radio configuration to your LoRa dongle, you only need to do this the first time you have set your radio parameters - see [lora_adapter_config.md](lora_adapter_config.md)

Run `LoRaQ-transmitter` to launch the transmitter. You will see a graphical preview including transmission and LoRa statistics.
