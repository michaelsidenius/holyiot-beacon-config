# holyiot-beacon-config

Configure **Holyiot** BLE beacons (Shenzhen Holyiot Technology, Nordic nRF51/nRF52-based) from a Linux PC, without the vendor's
phone app. Set the iBeacon major/minor, name, TX power, advertising interval, one-metre RSSI and frame type, and read
battery level and settings from the beacon's adverts.

The vendor's configuration protocol is not published. It was decoded from the app's Bluetooth traffic; see
[PROTOCOL.md](PROTOCOL.md) for the details, what is confirmed, and what is not.

Tested with two Holyiot iBeacons powered by a CR2477 coin cell, configured by the Android app "Holyiot-beacon"
(factory name `Holy-IOT`).
Other Holyiot models with the same app probably speak the same protocol, but that is untested.
Not affiliated with Holyiot. Use at your own risk.

## Install

Linux with BlueZ and a Bluetooth LE adapter that can hold a connection (not every cheap USB dongle can).

```sh
python3 -m venv ~/venv-holyiot
~/venv-holyiot/bin/pip install -r requirements.txt
```

## Use

```sh
# find Holyiot beacons nearby (factory name "Holy-IOT…" or the Holyiot status data in the scan response)
~/venv-holyiot/bin/python holyiot_config.py scan

# what one beacon sends: frame, UUID, major/minor, name, battery %, interval and TX steps
~/venv-holyiot/bin/python holyiot_config.py status C7:00:11:22:33:44
#   add --hci-timing to count every advert with btmon (needs sudo) and measure the real interval

# change settings (connects, logs in, writes, disconnects; the beacon applies them on disconnect)
~/venv-holyiot/bin/python holyiot_config.py set C7:00:11:22:33:44 minor=101 name=Car interval=4 tx=6
~/venv-holyiot/bin/python holyiot_config.py set C7:00:11:22:33:44 frame=ibeacon
```

| Key | Values |
|---|---|
| `name` | 1–12 ASCII characters |
| `major`, `minor` | 0–65535 |
| `mpower` | one-metre RSSI written into the iBeacon frame, e.g. `-59` |
| `tx` | TX power step 0–6 (6 is the strongest; 0 untested, see PROTOCOL.md) |
| `interval` | 0–6 = about 30, 60, 120, 300, 500, 750, 1000 ms |
| `frame` | `ibeacon` (Apple company ID, what Home Assistant's iBeacon integration wants), `beacon` (company ID 0xFFFF), `eddystone` (untested) |

### Password

The password is never given on the command line. It is taken from, in this order:

1. `--password-file` (default `~/.config/holyiot/passwords.txt`, see `passwords.example.txt`);
2. the environment variable `HOLYIOT_PASSWORD`;
3. a prompt;
4. the factory default `AA14061112` (turn off with `--no-factory-default`).

A wrong password is simply rejected by the beacon and nothing is written.

## Tips

- **Two beacons from the same batch can carry the same UUID/major/minor.** Give each its own minor first.
- Changing TX power: if the signal does not change, take the coin cell out and put it back once.
- All settings survive a battery change.
- Battery life: Holyiot quotes an average of about 25 µA at 1000 ms, 45 µA at 500 ms and 73 µA at 300 ms (+4 dBm).
  On a CR2032 (~220 mAh) that is roughly 12, 6.5 and 4 months; on a CR2477 (~1000 mAh) roughly 4.5, 2.5 and 1.5 years.

### Home Assistant

- Use `frame=ibeacon`: the iBeacon integration ignores other company IDs.
- After every restart the iBeacon integration waits for an advert **with a name** before it tracks a beacon again, and
  Holyiot sends the name only in the scan response. If your proxy hears the beacon weakly, that can take days.
  Fix: *Settings → Devices & services → iBeacon Tracker → Configure → add the UUID to "allow nameless UUIDs"*.
- After that, the tracker goes `unavailable → home` right after a restart. Give "arrived home" automations
  `not_from: [unavailable, unknown]`, or they fire on every restart.

## How it was decoded

Android: *Developer options → Bluetooth HCI snoop log → Enabled*, Bluetooth off/on, change one setting at a time in
the vendor app, then `adb bugreport` and `btmon -r FS/data/misc/bluetooth/logs/btsnoop_hci.log`.
Wait until the bug report is completely written before copying it: the phone's USB connection resets while it is made.

## Licence

MIT, see [LICENSE](LICENSE).
