# Smart home setup

Jarvis controls smart devices through **Home Assistant**, a free, open-source hub that runs on your own hardware and supports thousands of devices. If your devices speak **MQTT** (many ESP projects do), you can connect a broker instead or as well; see [MQTT only](#mqtt-only).

Everything stays on your home network. Jarvis never contacts a device manufacturer.

## 1. Run Home Assistant in VirtualBox on this PC

Home Assistant runs on this PC as a virtual machine. It works while the PC is on. When the PC is off or asleep, Home Assistant and its schedules stop too. Move to a Raspberry Pi or mini PC later with Home Assistant's backup and restore if you want it always on.

1. Install [VirtualBox](https://www.virtualbox.org/wiki/Downloads) for Windows.
2. Download the **VirtualBox (.vdi)** image from the [Home Assistant Windows install page](https://www.home-assistant.io/installation/windows) and unzip it.
3. In VirtualBox, choose **New**:
   - Name: `Home Assistant`. Type: Linux. Version: Other Linux (64-bit).
   - Memory: 2048 MB or more. Processors: 2.
   - Hard disk: **Use an existing virtual hard disk file**, pick the `.vdi` you unzipped.
4. Open the new machine's **Settings**:
   - **System**: tick **Enable EFI**.
   - **Network**: Adapter 1 attached to **Bridged Adapter**, using the network card this PC uses for the internet (Wi-Fi or Ethernet). Bridged mode lets Home Assistant find devices on your network.
   - **Audio**: untick Enable Audio.
5. Start the machine. After a few minutes the console shows a Home Assistant banner with its address, for example `http://192.168.1.50:8123`.
6. Open that address (or `http://homeassistant.local:8123`) in a browser and create your Home Assistant account. This account is local, on your PC.
7. In your router, reserve that address for the Home Assistant machine so it never changes.

To start Home Assistant without the VirtualBox window, right-click the machine and choose **Start > Headless Start**.

## 2. Add your devices to Home Assistant

Home Assistant often finds devices by itself: check **Settings > Devices & services** for "Discovered". Otherwise choose **Add integration** and search for the brand.

| Device | Integration |
|--------|-------------|
| Govee lights | **Govee lights local**. Turn on *LAN Control* for each light in the Govee app first |
| Feit and other "Smart Life" devices | **Tuya**. Signs in to the Tuya cloud, so these devices need the internet |
| Philips Hue | **Philips Hue** (local, through the bridge) |
| TP-Link Kasa / Tapo | **TP-Link Smart Home** (local) |
| Matter devices | **Matter** |
| ESP boards you program | **ESPHome** (local, recommended for your own projects) |
| WLED light strips | **WLED** (local) |

Whether a device works offline depends on the device, not on Jarvis.

## 3. Choose what Jarvis may control

In Home Assistant, open **Settings > Voice assistants > Expose** and expose the devices Jarvis should see. Jarvis ignores everything else. Give each device a clear name and a room (area). That is what you say to Jarvis: "turn off the bedroom lamp".

## 4. Connect Jarvis

1. In Home Assistant, signed in as an **administrator** (the account you created first is one), click your name (bottom left), open **Security**, and under **Long-lived access tokens** choose **Create token**. Copy it. Home Assistant only tells administrators which devices are exposed, so a token from a non-administrator account does not work.
2. In Jarvis, open **Settings > Smart Home**, turn it on, enter the Home Assistant address and paste the token.
3. Press **Test connection**. Jarvis shows how many devices it can see.

The token is stored in Windows Credential Manager, never in Jarvis's config file.

Try "turn on the living room lights" or "what's the temperature in the bedroom".

## MQTT only

If your devices publish Home Assistant MQTT discovery messages (ESPHome, Tasmota, WLED and Zigbee2MQTT can), Jarvis can use them straight from an MQTT broker such as [Mosquitto](https://mosquitto.org/). In **Settings > Smart Home > MQTT**, enter the broker's address, port and login, then press **Test connection**. If you also run Home Assistant, connect devices through Home Assistant instead; Jarvis hides MQTT copies of devices Home Assistant already has.

## Safety

Lights, plugs, fans, blinds and scenes change straight away. Jarvis always asks before it unlocks a door, disarms an alarm, opens a garage door or gate, runs a script, or changes more than 10 devices at once. You can make any device always ask in **Settings > Smart Home > Devices**.
