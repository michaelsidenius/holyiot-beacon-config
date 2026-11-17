#!/usr/bin/env python3
"""Configure Holyiot BLE beacons without the vendor app.

Works with Holyiot (Shenzhen Holyiot Technology) nRF52-based beacons whose vendor app is
"Holyiot-beacon" (Android com.jx.bledemo). The protocol is described in PROTOCOL.md.

Commands
  scan   [seconds]                   list Holyiot-looking beacons nearby
  status <MAC> [seconds]             what one beacon advertises (frame, ids, name, battery, TX/interval index)
  set    <MAC> key=value ...         connect, log in, write the values, disconnect (changes apply on disconnect)

Keys for `set`
  name=<text, max 12 ASCII>   major=<0..65535>   minor=<0..65535>   mpower=<one-metre RSSI, e.g. -59>
  tx=<0..6 TX power step>     interval=<0..6 advertising interval step>   frame=beacon|ibeacon|eddystone

Password (never on the command line), tried in this order:
  1. --password-file: lines "[label] <MAC>: <digits>", "[label] <MAC>: same-as <other MAC>", "default: <digits>"
  2. the environment variable HOLYIOT_PASSWORD
  3. an interactive prompt (if nothing above matched and stdin is a terminal)
  4. the factory default AA14061112 (skip with --no-factory-default)
A wrong password is simply rejected by the beacon (answer 01) and nothing is written.
"""
import argparse
import asyncio
import getpass
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time

from bleak import BleakClient, BleakScanner

__version__ = "0.1.0"

NUS_RX = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"  # we write commands here
NUS_TX = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"  # the beacon answers here (notify)
CMD = {"password": 0x00, "major": 0x03, "minor": 0x04, "name": 0x05, "tx": 0x06, "interval": 0x07,
       "mpower": 0x08, "frame": 0x2F}
FRAMES = {"beacon": 0, "ibeacon": 1, "eddystone": 2}  # 0 and 1 confirmed, 2 assumed
FACTORY_PASSWORD = "AA14061112"
INTERVAL_MS = {0: 30, 1: 60, 2: 120, 3: 300, 4: 500, 5: 750, 6: 1000}  # measured, approximate
STATUS_UUID = "00005242-0000-1000-8000-00805f9b34fb"


# ---------------------------------------------------------------- passwords

def _from_file(mac, path, depth=0):
    if not path or not os.path.exists(path):
        return None
    txt = open(path).read()
    m = re.search(rf"^[^\n#]*?\b{re.escape(mac.upper())}\s*:\s*(?:same-as\s+([0-9A-Fa-f:]{{17}})|([0-9A-Fa-f]+))\b", txt, re.M | re.I)
    if m and m.group(1) and depth < 3:
        return _from_file(m.group(1).upper(), path, depth + 1)
    if m and m.group(2):
        return m.group(2)
    d = re.search(r"^[^\n#]*?\bdefault\s*:\s*([0-9A-Fa-f]+)\b", txt, re.M | re.I)
    return d.group(1) if d else None


def password_candidates(mac, path, use_factory=True):
    cands = [_from_file(mac, path), os.environ.get("HOLYIOT_PASSWORD")]
    if not any(cands) and sys.stdin.isatty():
        typed = getpass.getpass(f"Password for {mac} (empty = factory default): ").strip()
        cands.append(typed or None)
    if use_factory:
        cands.append(FACTORY_PASSWORD)
    out = [c.upper() for c in dict.fromkeys(c for c in cands if c)]
    for c in out:
        if len(c) % 2 or not re.fullmatch(r"[0-9A-F]+", c):
            raise SystemExit("a password must be an even number of hex digits (e.g. AA14061112)")
    return [bytes.fromhex(c) for c in out]


# ---------------------------------------------------------------- encoding

def frame(cmd, data):
    return bytes([0xF3, cmd, 0xF3, len(data)]) + data


def encode(key, val):
    if key == "name":
        b = val.encode("ascii")
        if not 0 < len(b) <= 12:
            raise SystemExit("name: 1..12 ASCII characters")
        return b + b"\x00" * (12 - len(b))
    if key in ("major", "minor"):
        v = int(val)
        if not 0 <= v <= 65535:
            raise SystemExit(f"{key}: 0..65535")
        return v.to_bytes(2, "big")
    if key in ("tx", "interval"):
        v = int(val)
        if not 0 <= v <= 6:
            raise SystemExit(f"{key}: 0..6")
        if key == "tx" and v == 0:
            print("warning: tx=0 is untested; the beacon may become too weak to reconnect to")
        return bytes([v])
    if key == "mpower":
        v = int(val)
        if not -127 <= v <= 0:
            raise SystemExit("mpower: -127..0 (dBm at one metre)")
        return v.to_bytes(1, "big", signed=True)
    if key == "frame":
        if val.lower() not in FRAMES:
            raise SystemExit("frame: beacon, ibeacon or eddystone")
        return bytes([FRAMES[val.lower()]])
    raise SystemExit(f"unknown key {key}")


# ---------------------------------------------------------------- set

async def do_set(mac, pairs, adapter, pwfile, use_factory):
    answers = asyncio.Queue()
    async with BleakClient(mac, timeout=30, bluez={"adapter": adapter}) as c:
        await c.start_notify(NUS_TX, lambda _, d: answers.put_nowait(bytes(d)))

        async def send(cmd, data, label):
            await c.write_gatt_char(NUS_RX, frame(cmd, data), response=False)
            try:
                a = await asyncio.wait_for(answers.get(), 4)
            except asyncio.TimeoutError:
                print(f"  {label}: no answer")
                return False
            ok = len(a) >= 4 and a[0] == 0xF3 and a[1] == cmd and a[3] == 0
            print(f"  {label}: {'OK' if ok else 'rejected'} ({a.hex(' ')})")
            return ok

        for n, pw in enumerate(password_candidates(mac, pwfile, use_factory), 1):
            if await send(CMD["password"], pw, f"password (candidate {n})"):
                break
        else:
            raise SystemExit("no password accepted - nothing written")
        for k, v in pairs:
            await send(CMD[k], encode(k, v), f"{k}={v}")
            await asyncio.sleep(0.5)
    print("disconnected - the new values apply now")


# ---------------------------------------------------------------- scan / status

async def _listen(adapter, seconds):
    seen = {}

    def cb(dev, adv):
        s = seen.setdefault(dev.address.upper(), {"rssi": [], "mfr": {}, "svc": {}, "name": None})
        s["rssi"].append(adv.rssi)
        if adv.local_name:
            s["name"] = adv.local_name.rstrip("\x00")
        s["mfr"].update(adv.manufacturer_data)
        s["svc"].update(adv.service_data)

    async with BleakScanner(cb, scanning_mode="active", bluez={"adapter": adapter}):
        await asyncio.sleep(seconds)
    return seen


def describe(s):
    out = {}
    for company, m in s["mfr"].items():
        if len(m) >= 23 and m[0] == 0x02 and m[1] == 0x15:
            u = m[2:18].hex()
            out.update(frame="iBeacon (Apple 0x004C)" if company == 0x004C else f"beacon (company 0x{company:04X})",
                       uuid=f"{u[:8]}-{u[8:12]}-{u[12:16]}-{u[16:20]}-{u[20:]}",
                       major=int.from_bytes(m[18:20], "big"), minor=int.from_bytes(m[20:22], "big"),
                       mpower=int.from_bytes(m[22:23], "big", signed=True))
    if s["name"]:
        out["name"] = s["name"]
    sd = s["svc"].get(STATUS_UUID)
    if sd and len(sd) >= 10:
        out.update(battery_pct=sd[1], interval_step=sd[8], tx_step=sd[9])
        out["interval_ms~"] = INTERVAL_MS.get(sd[8])
    if s["rssi"]:
        out["rssi_median"] = statistics.median(s["rssi"])
    return out


def hci_timing(adapter, seconds, mac):
    """Optional: record with btmon (needs sudo) to count every advert and measure the real interval."""
    if not shutil.which("btmon"):
        return {}
    d = tempfile.mkdtemp()
    cap = os.path.join(d, "cap.btsnoop")
    mon = subprocess.Popen(["sudo", "-n", "btmon", "-i", adapter, "-w", cap],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1)
    asyncio.run(_listen(adapter, seconds))
    subprocess.run(["sudo", "-n", "kill", "-INT", str(mon.pid)], stderr=subprocess.DEVNULL)
    time.sleep(1)
    txt = subprocess.run(["sudo", "-n", "btmon", "-r", cap], capture_output=True, text=True).stdout
    times = []
    for blk in re.split(r"\n(?=[>@<] )", txt):
        if not blk.startswith("> HCI Event: LE Meta Event"):
            continue
        t = re.search(r"#\d+ (?:\[\w+\] )?([\d.]+)\s*$", blk.splitlines()[0])
        for rep in re.split(r"\n\s+Event type:", blk)[1:]:
            if f"Address: {mac}" in rep and "SCAN_RSP" not in rep and t:
                times.append(float(t.group(1)))
    gaps = [b - a for a, b in zip(times, times[1:]) if b - a > 0.015]
    out = {"adverts_received": len(times)}
    if gaps:
        out["advert_gap_median_ms"] = round(statistics.median(gaps) * 1000)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["scan", "status", "set"])
    ap.add_argument("args", nargs="*")
    ap.add_argument("--adapter", default="hci0", help="BlueZ adapter (default hci0)")
    ap.add_argument("--password-file", default=os.path.expanduser("~/.config/holyiot/passwords.txt"))
    ap.add_argument("--no-factory-default", action="store_true", help="do not try AA14061112")
    ap.add_argument("--hci-timing", action="store_true", help="status: count adverts with btmon (sudo)")
    ap.add_argument("--version", action="version", version=__version__)
    a = ap.parse_args()

    if a.command == "scan":
        secs = float(a.args[0]) if a.args else 10
        seen = asyncio.run(_listen(a.adapter, secs))
        hits = {m: describe(s) for m, s in seen.items()
                if STATUS_UUID in s["svc"] or (s["name"] or "").lower().startswith("holy")}
        for mac, d in sorted(hits.items()):
            print(mac, " ".join(f"{k}={v}" for k, v in d.items()))
        if not hits:
            print("no Holyiot-looking beacon heard")
        return 0

    if not a.args or not re.fullmatch(r"[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}", a.args[0]):
        raise SystemExit("give the beacon's MAC address, e.g. C7:00:11:22:33:44")
    mac = a.args[0].upper()

    if a.command == "status":
        secs = float(a.args[1]) if len(a.args) > 1 else 10
        if a.hci_timing:
            t = hci_timing(a.adapter, secs, mac)
            seen = asyncio.run(_listen(a.adapter, 4))
        else:
            t, seen = {}, asyncio.run(_listen(a.adapter, secs))
        if mac not in seen:
            print("not heard")
            return 1
        d = describe(seen[mac]) | t
        print(mac, " ".join(f"{k}={v}" for k, v in d.items()))
        return 0

    pairs = [p.split("=", 1) for p in a.args[1:]]
    if not pairs or any(len(p) != 2 or p[0] not in CMD or p[0] == "password" for p in pairs):
        raise SystemExit("set <MAC> key=value ...  (keys: " + ", ".join(k for k in CMD if k != "password") + ")")
    for k, v in pairs:
        encode(k, v)  # validate everything before connecting
    asyncio.run(do_set(mac, pairs, a.adapter, a.password_file, not a.no_factory_default))
    return 0


if __name__ == "__main__":
    sys.exit(main())
