## Overview

The recommended receiver setup is a Raspberry Pi Zero with a Waveshare SX1262 based LoRa HAT fitted, connected via HDMI to a 10.1 inch HDMI monitor with integral speakers which also provides power for the Pi via an integral USB power socket.  

These monitors are widely available as reverse cameras for vehicle use, generally the 10.1 inch versions include speakers and USB power out but smaller versions may not have sound or USB ports and only composite video input, so be careful when choosing. They run on 12v which can be useful in portable situations where they need to be battery powered (e.g. the cab of a steam locomotive) and draw around 300mA continuous including the Rpi and LoRa adapter so can be used with for example a 5Ah Li-ion battery pack to give well over 10 hours battery life for complete portability.  

DietPi OS on the Raspberry Pi is recommended due to its optimised size and fast boot and the instructions below are based on this, but it will also run on standard Raspberry OS Lite. For best graphic performance given the low power of the Pi Zero, it is recommended to use an HDMI output resolution of 800 x 480 pixels.

## Receiver Setup & Pi Installation (`LoRaQ-display`)

### Prerequisites (Raspberry Pi DietPi Trixie)

First use an image writer utility to write the `DietPi_RPi1-ARMv6-Trixie.img.xz` image file to your SD Card.  

DietPi allows various configuration parameters to be set in `dietpi.txt`, `cmdline.txt` and `config.txt` files in the boot partition of the SD card, which will then be applied on the first boot of the OS.  
Wifi networking should be setup so that the DietPi OS can update itself on first boot, and install additional packages such as Alsa which are required for audio to work, and OpenSSH to allow remote login and remote file copy.
An additional file `dietpi-wifi.txt` allows you to pre-configure your wifi details which are applied at first boot and should also be copied to the SD Card prior to first boot. Ensure to edit this with your specific wifi details before copying below. You can use the site https://www.wireshark.org/tools/wpa-psk to generate the WPA key from your SSID and password.

If this wifi will not be available at the installation location then you may wish to use details of a personal hotspot from a phone or other portable device, so that you can switch on your hotspot to ssh into the Pi when on site.  
OR you can enable a hotspot on the Pi by configuring the options within `dietpi.txt`, and connect to that for future config changes. 

Copy the following files from `/dietpi-config/` to the FAT partition of the SD Card prior to inserting it into the Pi:

* `dietpi.txt`
* `cmdline.txt`
* `config.txt`
* `dietpi-wifi.txt`

These files set various default parameters such as enabling sound, and the 800x480 screen resolution.

First boot of the Pi will take some time as the default settings are applied, and packages updated.  

When the system has finished booting, ssh into the Pi as `root`, with the password `dietpi`. At first ssh login it will ask you to change the root password - change it to one of your choice.

Create user `loraq` with password `loraq`:
```bash
sudo adduser loraq
sudo usermod -aG dietpi,sudo,video,audio,input,dialout loraq
```

Logout of `root`, log back in as `loraq`.

------------------

Run: 
```bash
dietpi-config

Navigate to Advanced Options: Serial/UART, Enable Mini UART
Reboot when prompted
```

Copy the following into `/home/loraq/`:

* `LoRaQ-display`
* `loraq.ini`
* `LoRaQ-adapter-configure`
* `dietpi-config/loraq-display.service`

Now edit `loraq.ini` (or edit it externally then copy to the Pi) as this will need to be customised to this device.

It is important that all the `[radio]` settings in `loraq.ini` remain the same as when the LoRa adapter on the transmitter was programmed, as these values are used to calculate RF duty-cycle and dwell time etc, and must match between transmitter and receiver. 

### LoRa HAT on the GPIO Header

The Waveshare SX1262 LoRa HAT uses the Pi's built-in UART instead of a USB port when the config jumpers are set to position B.

Ensure the M0/M1 mode pins or jumpers are set normal (transparent) operation i.e. both LOW.
You can set the interface type and port to auto in `loraq.ini`.

```ini
[link]
interface = auto
port = 
```

Carry out the adapter programming on this receiver - see [lora_adapter_config.md](lora_adapter_config.md) for info on programming the LoRa device.


### HDMI Sound

Audio alerts are sent over the HDMI connection to the attached monitor, so ensure you use a monitor with integrated speakers or an HDMI audio extractor if you want to use an external amplifier. Set audio_enabled to false to mute local HDMI alerts.

```ini
[receiver]
audio_enabled = false   
```

### Station Identity & ACK Slot
Every receiver and transmitter needs a **unique station ID and a unique ACK slot** (0-31), so that ACKs from different stations don't collide. Set these in its own `[station]` section. Both identity values are mandatory: missing, blank or invalid values will prevent the application from starting. IDs contain 1-16 ASCII letters, digits, `_` or `-`.

```ini
[station]
station_id = Steam_loco
ack_slot = 0
```

In `[receiver]`, there is an optional full-width top bar with the incoming command's TX ID and RSSI that can appear on the display. This can be useful when multiple transmitters are in use so that you can see where the message came from. Set `show_tx_info` = `true` to show the bar.

```ini
[receiver]
show_tx_info = true
```


---

### Auto-Start the display service

Configure `LoRaQ-display` to launch automatically on boot:

1. Copy the systemd service file:
   ```bash
   sudo cp loraq-display.service /etc/systemd/system/
   ```

2. Enable and start the service:
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable loraq-display.service
   sudo systemctl start loraq-display.service
   ```
Then reboot, and the LoRaQ-display should automatically start up.


> **Note:** Only one LoRaQ process can open the serial port at a time. To run the display by hand for debugging, first `sudo systemctl stop loraq-display.service`. You can also follow the service's log using `journalctl -u loraq-display.service -f`.
