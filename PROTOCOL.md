# Holyiot beacon configuration protocol

Reverse-engineered in November 2026 from the vendor app **Holyiot-beacon** (Android `com.jx.bledemo`) talking to a
Holyiot iBeacon powered by a CR2477 coin cell (factory name `Holy-IOT`; Nordic nRF51/nRF52, exact chip not checked). Method: Android *Developer options → Bluetooth HCI snoop log →
Enabled*, one setting changed at a time in the app, `adb bugreport`, then `btmon -r` on
`FS/data/misc/bluetooth/logs/btsnoop_hci.log`. Every command below was then replayed from Linux with
`holyiot_config.py` and checked over the air.

Status: **confirmed** = seen in the app's traffic and verified by replay; **assumed** = not verified.

## Transport

The beacon is connectable and exposes only the Generic Attribute service and the **Nordic UART Service** (no Device
Information, no DFU service):

| | UUID | Handle seen |
|---|---|---|
| Service | `6e400001-b5a3-f393-e0a9-e50e24dcca9e` | |
| RX (we write, write-without-response) | `6e400002-b5a3-f393-e0a9-e50e24dcca9e` | 0x000d |
| TX (beacon notifies) | `6e400003-b5a3-f393-e0a9-e50e24dcca9e` | 0x000f, CCCD 0x0010 |

Enable notifications on TX first.

## Framing

```
request:  F3 <cmd> F3 <len> <data[len]>
answer:   F3 <cmd> F3 <status>          status 00 = OK, 01 = rejected
```

The password command must come first in every connection. **New values take effect when the central disconnects**
(seen for minor, major, frame type, interval and TX power).

## Commands

"Verified" = seen in the app's traffic and replayed from Linux; "app code" = read from the decompiled app
(Holyiot-beacon 12.6.0), not tried on hardware. Commands without data are sent as just `F3 <cmd> F3` (no length byte).

| cmd | Setting | Data | Status |
|---|---|---|---|
| `00` | log in with the password (must come first) | the password's hex digits packed two per byte, as typed: `AA14061112` → `AA 14 06 11 12`; a 4-digit password → 2 bytes | verified |
| `01` | change the password | 6 bytes: the new password's hex digits packed, padded with `00` (the manual: "AA1406111200, the last two 0s can be omitted") | app code |
| `02` | UUID | 16 bytes | app code |
| `03` | major | 2 bytes, big-endian | verified |
| `04` | minor | 2 bytes, big-endian | verified |
| `05` | device name | 12 bytes, UTF-8, cut at 12 characters, NUL-padded | verified |
| `06` | TX power | 1 byte, slider step 0–6 (see below) | verified (1–6) |
| `07` | advertising interval | 1 byte, step 0–6 (see below) | verified |
| `08` | one-metre RSSI (measured power in the iBeacon frame) | 1 byte, signed, the app allows −128…127 (`C5` = −59) | verified |
| `09` | MAC address | 6 bytes | app code |
| `10`–`16` | read a sensor, no data: `10` temperature, `11` air pressure, `12` humidity, `13` vibration, `14` state, `16` battery (`15` handled in the answer, no button) | — | app code; a beacon without the sensor does not answer `10` (seen) |
| `17` | "External equipment" on/off | 1 byte, 0/1 | app code |
| `2F` | frame type | `00` = "Beacon" (iBeacon layout under company ID 0xFFFF), `01` = iBeacon (Apple 0x004C), `02` = Eddystone | 00, 01 verified; 02 app code |
| `20`–`2E` | Eddystone settings: `20` UID advertise, `21` URL advertise, `22` TLM advertise, `23` EID advertise, `24` UID name, `25` namespace, `26` instance, `27` URL name, `28` URL prefix, `29` URL content, `2A` URL suffix, `2B` TLM name, `2C` TLM version, `2D` EID name, `2E` encrypted ID | see the app | app code |

Sensor answers (app code): `10` → `F3 10 F3 00 <a> <b>` shown as `a.b` °C; `11` → pressure `(a<<8 | b) + 80000` Pa;
`12` → humidity `a` %; `13` → `(a<<8 | b)`, `0x5555` = no vibration.

### Interval steps (measured advert spacing)

| Step | 0 | 1 | 2 | 3 | 4 | 5 | 6 |
|---|---|---|---|---|---|---|---|
| Interval | ~30 ms | ~60 ms | ~120 ms | ~300 ms | ~500 ms (factory) | ~750 ms | ~1000 ms |

### TX steps (received signal at 1 m relative to step 6, two passes)

| Step | 1 | 2 | 3 | 4 | 5 | 6 |
|---|---|---|---|---|---|---|
| Relative | −11 dB | −4.5 dB | −4 dB | −1 dB | −1.5 dB | 0 (probably +4 dBm, the chip's maximum) |

Step 0 was not tried: it may make the beacon too weak to reconnect to.
**Observed once:** right after configuring with the app, TX changes had no effect until the coin cell was taken out and
put back; after that, TX changed on every disconnect. All settings survive a battery change.

## What the beacon sends

Advertising packet (connectable, `ADV_IND`), 30 bytes:

```
02 01 06                          flags
1A FF <company LE> 02 15          manufacturer data: 0x004C (iBeacon frame) or 0xFFFF ("Beacon" frame)
   <UUID 16> <major 2> <minor 2> <measured power 1>
```

Scan response (only to active scanners):

```
xx 09 <name, 12 bytes, NUL-padded>
xx 16 42 52 41 <battery %> <MAC 6 bytes> <interval step> <TX step> 06 00 00     service data, 16-bit UUID 0x5242
```

The name is **only** in the scan response: the iBeacon advert leaves one byte free.

## Factory defaults

The app has **no default names** and writes the name exactly as typed, so the factory name comes from the beacon's
firmware. Holyiot's manuals give **`Holy-IOT`** for the current models (one table writes `HOLY-IOT`) and **`holyiot`**
(1–14 bytes) for the older CR2477 "YN" series. Other defaults: password `AA14061112`, UUID
`FDA50693-A4E2-4FB1-AFCF-C6EB07647825`, major 10011 (newer) or 10032 (YN series); our units had minor 19641, measured
power −55 (0xFFFF frame) or −65 (Apple frame), interval step 4 (500 ms). **Two units shipped with identical
UUID/major/minor**: give each its own minor before using both.

## Home Assistant notes

- HA's **iBeacon Tracker** only matches Apple's company ID (0x004C), so a beacon in "Beacon" (0xFFFF) mode is ignored.
- After every HA restart, the iBeacon integration ignores a beacon until an advert **with a name** arrives, unless its
  UUID is in the integration's option *"allow nameless UUIDs"*. With a weak signal the name (scan response) may take
  days to get through. Allowlisting the UUID fixes it.
- With that fix the tracker returns `unavailable → home` seconds after a restart. Automations that trigger on
  `to: home` should add `not_from: [unavailable, unknown]`, or they fire on every restart.
