#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# NetUsage - monthly download/upload history for Windows
# Copyright (C) 2026 Sakura (https://github.com/sakurarose1)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""
NetUsage - a permanent, month-by-month record of how much you download and
upload on this Windows PC, across every network, with a per-app breakdown.

Where the numbers come from
---------------------------
Windows already measures every app's traffic, every hour, on every network,
in its "System Resource Usage Monitor" database (SRUM):
    C:\\Windows\\System32\\sru\\SRUDB.dat
That is what Settings > Network > Data usage reads - but Settings only shows a
rolling 30-day slice split per network, and Windows deletes old rows after
roughly 30-60 days.

NetUsage copies that database once an hour (as a background task running as
SYSTEM), adds any new rows to its own history file, and never deletes them.
It then writes a dashboard page you can open at any time.

Commands
--------
    python netusage.py install            (as administrator) set everything up
    python netusage.py uninstall [--purge]  remove the task (and the data with --purge)
    python netusage.py collect            import new rows + rebuild the dashboard
    python netusage.py report [--open]    rebuild the dashboard only
    python netusage.py status             show what has been recorded so far

Files live in C:\\ProgramData\\NetUsage  (override with the NETUSAGE_DATA env var).
"""

import sys
import os
import struct

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "lib"))

import argparse
import datetime as dt
import json
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time

VERSION = "1.1"
SOURCE_URL = "https://github.com/sakurarose1/NetUsage"
IS_WINDOWS = os.name == "nt"

DATA_DIR = os.environ.get("NETUSAGE_DATA") or os.path.join(
    os.environ.get("ProgramData", r"C:\ProgramData"), "NetUsage")
DB_PATH = os.path.join(DATA_DIR, "history.sqlite")
DASH_PATH = os.path.join(DATA_DIR, "dashboard.html")
LOG_PATH = os.path.join(DATA_DIR, "collector.log")
APP_DIR = os.path.join(DATA_DIR, "app")
TEMPLATE_PATH = os.path.join(HERE, "dashboard_template.html")

_WIN = os.environ.get("SystemRoot", r"C:\Windows")
# 32-bit Python on 64-bit Windows sees SysWOW64 instead of System32; "Sysnative" is the way through.
_SYS32 = os.path.join(_WIN, "Sysnative") if (IS_WINDOWS and struct.calcsize("P") == 4
                                              and os.path.isdir(os.path.join(_WIN, "Sysnative"))) else os.path.join(_WIN, "System32")
SRUM_PATH = os.path.join(_SYS32, "sru", "SRUDB.dat")
ESENTUTL = os.path.join(_SYS32, "esentutl.exe")
NET_TABLE = "{973F5D5C-1D90-4944-BE8E-24B94231A174}"   # SRUM "Network Data Usage"
TASK_NAME = "NetUsage Collector"
SHORTCUT_NAME = "Data Usage.url"

NO_WINDOW = 0x08000000 if IS_WINDOWS else 0


# --------------------------------------------------------------------------- logging

def log(msg):
    line = "%s  %s" % (dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        print(line)
    except Exception:
        pass   # pythonw has no console
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 512 * 1024:
            with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
                tail = f.readlines()[-1500:]
            with open(LOG_PATH, "w", encoding="utf-8") as f:
                f.writelines(tail)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def run(cmd, timeout=120):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                          creationflags=NO_WINDOW, errors="replace")


# --------------------------------------------------------------------------- history db

SCHEMA = """
CREATE TABLE IF NOT EXISTS apps (
    id   INTEGER PRIMARY KEY,
    key  TEXT UNIQUE NOT NULL           -- normalised SRUM app identity
);
CREATE TABLE IF NOT EXISTS networks (
    id      INTEGER PRIMARY KEY,
    luid    INTEGER NOT NULL,           -- network adapter id (top 16 bits = adapter type)
    profile INTEGER NOT NULL,           -- Wi-Fi profile index (0 = none)
    iftype  INTEGER,
    alias   TEXT,                       -- adapter name, e.g. "Wi-Fi", "Ethernet 2"
    ssid    TEXT,                       -- Wi-Fi network name
    UNIQUE (luid, profile)
);
CREATE TABLE IF NOT EXISTS usage (
    rec   INTEGER NOT NULL,             -- SRUM AutoIncId
    ts    INTEGER NOT NULL,             -- end of the measured interval, unix seconds UTC
    app   INTEGER NOT NULL REFERENCES apps(id),
    net   INTEGER NOT NULL REFERENCES networks(id),
    sid   TEXT NOT NULL,                -- Windows account the traffic belonged to
    sent  INTEGER NOT NULL,             -- bytes uploaded
    recv  INTEGER NOT NULL,             -- bytes downloaded
    UNIQUE (rec, ts, app, net, sid)
);
CREATE INDEX IF NOT EXISTS usage_ts ON usage(ts);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""


def open_db(readonly=False):
    if readonly:
        uri = "file:%s?mode=ro" % DB_PATH.replace("\\", "/")
        return sqlite3.connect(uri, uri=True)
    os.makedirs(DATA_DIR, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.executescript(SCHEMA)
    return con


def meta_get(con, k, default=None):
    row = con.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return row[0] if row else default


def meta_set(con, k, v):
    con.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))


# --------------------------------------------------------------------------- copying SRUM

def copy_srum(dest_dir, method):
    """SRUDB.dat is held open by Windows, so it cannot simply be copied.
    'shadow copy' uses esentutl /vss; 'service restart' briefly stops the
    Diagnostic Policy Service that owns the file, copies it, and starts it again."""
    dest = os.path.join(dest_dir, "SRUDB.dat")
    if os.path.exists(dest):
        os.remove(dest)
    if method == "shadow copy":
        r = run([ESENTUTL, "/y", SRUM_PATH, "/vss", "/d", dest], timeout=300)
        if r.returncode == 0 and os.path.exists(dest):
            return dest
        raise RuntimeError("esentutl /vss failed (code %s): %s"
                           % (r.returncode, (r.stdout + r.stderr).strip()[-300:]))
    run(["sc.exe", "stop", "DPS"])
    try:
        for _ in range(40):
            if "STOPPED" in run(["sc.exe", "query", "DPS"]).stdout:
                break
            time.sleep(0.5)
        shutil.copy2(SRUM_PATH, dest)
    finally:
        run(["sc.exe", "start", "DPS"])
    return dest


# --------------------------------------------------------------------------- reading SRUM

def ole_to_unix(v):
    """SRUM stores time as an OLE date (days since 1899-12-30, UTC)."""
    if v is None:
        return None
    if isinstance(v, dt.datetime):
        if v.tzinfo is None:
            v = v.replace(tzinfo=dt.timezone.utc)
        return int(v.timestamp())
    if isinstance(v, int):                      # raw 64-bit pattern of the double
        v = struct.unpack("<d", struct.pack("<q", v))[0]
    return int(round((float(v) - 25569.0) * 86400))


def sid_to_str(b):
    if not b or len(b) < 8:
        return ""
    rev, count = b[0], b[1]
    auth = int.from_bytes(b[2:8], "big")
    subs = [struct.unpack("<I", b[8 + 4 * i: 12 + 4 * i])[0]
            for i in range(count) if 12 + 4 * i <= len(b)]
    return "S-%d-%d" % (rev, auth) + "".join("-%d" % s for s in subs)


TOTAL_KEY = "sys:unattributed"   # app-less row = Windows' hourly total for a network


def app_key(idtype, blob):
    """Turn a SRUM id-map entry into a stable identity string."""
    if not blob:
        return TOTAL_KEY
    text = blob.decode("utf-16-le", "replace").rstrip("\x00").strip()
    if idtype == 1:
        return "svc:" + text
    if idtype == 2:
        # Package full name: Name_Version_Arch_ResourceId_PublisherId -> drop version/arch
        parts = text.split("_")
        if len(parts) >= 5:
            return "pkg:%s_%s" % (parts[0], parts[-1])
        return "pkg:" + text
    if text.startswith("!!"):                   # "!!SearchApp.exe!2020/01/01:..." form
        return "exe:" + text[2:].split("!")[0].lower()
    return "exe:" + text.lower()


def read_srum(path):
    from dissect.esedb import EseDB
    with open(path, "rb") as fh:
        db = EseDB(fh)
        idmap = {}
        for r in db.table("SruDbIdMapTable").records():
            idmap[r.get("IdIndex")] = (r.get("IdType"), r.get("IdBlob"))
        out = []
        for r in db.table(NET_TABLE).records():
            app_id, user_id = r.get("AppId"), r.get("UserId")
            itype, iblob = idmap.get(app_id, (0, None))
            utype, ublob = idmap.get(user_id, (3, None))
            out.append(dict(
                rec=r.get("AutoIncId") or 0,
                ts=ole_to_unix(r.get("TimeStamp")),
                app=app_key(itype, iblob),
                sid=sid_to_str(ublob) if utype == 3 else "",
                luid=r.get("InterfaceLuid") or 0,
                profile=r.get("L2ProfileId") or 0,
                sent=r.get("BytesSent") or 0,
                recv=r.get("BytesRecvd") or 0,
            ))
    return out


# --------------------------------------------------------------------------- naming networks

def wifi_profile_names():
    """Map SRUM's L2ProfileId -> Wi-Fi network name, from the WLAN service's registry."""
    names = {}
    if not IS_WINDOWS:
        return names
    import winreg
    base = r"SOFTWARE\Microsoft\WlanSvc\Interfaces"
    xml_root = os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"),
                            "Microsoft", "Wlansvc", "Profiles", "Interfaces")
    try:
        k_ifaces = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base)
    except OSError:
        return names
    for i in range(1000):
        try:
            iface = winreg.EnumKey(k_ifaces, i)
        except OSError:
            break
        try:
            k_profiles = winreg.OpenKey(k_ifaces, iface + r"\Profiles")
        except OSError:
            continue
        for j in range(1000):
            try:
                prof = winreg.EnumKey(k_profiles, j)
            except OSError:
                break
            index, name = None, None
            for sub in ("", r"\MetaData"):
                try:
                    k = winreg.OpenKey(k_profiles, prof + sub)
                except OSError:
                    continue
                try:
                    index = index if index is not None else winreg.QueryValueEx(k, "ProfileIndex")[0]
                except OSError:
                    pass
                if sub and name is None:
                    try:
                        hints = winreg.QueryValueEx(k, "Channel Hints")[0]
                        n = int.from_bytes(hints[0:4], "little")
                        if 0 < n <= 32:
                            name = hints[4:4 + n].decode("utf-8", "replace")
                    except OSError:
                        pass
            xml = os.path.join(xml_root, iface, prof + ".xml")
            try:
                with open(xml, "r", encoding="utf-8", errors="replace") as f:
                    m = re.search(r"<name>(.*?)</name>", f.read())
                    if m:
                        name = m.group(1)
            except OSError:
                pass
            if index is not None and name:
                names[int(index)] = name
    return names


def adapter_alias(luid):
    if not IS_WINDOWS:
        return None
    import ctypes
    buf = ctypes.create_unicode_buffer(257)
    l = ctypes.c_uint64(luid)
    try:
        if ctypes.windll.iphlpapi.ConvertInterfaceLuidToAlias(ctypes.byref(l), buf, 257) == 0:
            return buf.value or None
    except Exception:
        pass
    return None


# --------------------------------------------------------------------------- collect

def import_rows(con, rows):
    app_ids = dict(con.execute("SELECT key, id FROM apps"))
    net_ids = {(l, p): i for i, l, p in con.execute("SELECT id, luid, profile FROM networks")}
    wifi = None
    before = con.execute("SELECT COUNT(*) FROM usage").fetchone()[0]
    for r in rows:
        if r["ts"] is None:
            continue
        a = app_ids.get(r["app"])
        if a is None:
            a = con.execute("INSERT INTO apps(key) VALUES(?)", (r["app"],)).lastrowid
            app_ids[r["app"]] = a
        nk = (r["luid"], r["profile"])
        n = net_ids.get(nk)
        if n is None:
            n = con.execute("INSERT INTO networks(luid, profile, iftype) VALUES(?,?,?)",
                            (r["luid"], r["profile"], (r["luid"] >> 48) & 0xFFFF)).lastrowid
            net_ids[nk] = n
        con.execute(
            "INSERT INTO usage(rec, ts, app, net, sid, sent, recv) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(rec, ts, app, net, sid) DO UPDATE SET sent=excluded.sent, recv=excluded.recv",
            (r["rec"], r["ts"], a, n, r["sid"], r["sent"], r["recv"]))
    # Fill in / refresh adapter and Wi-Fi names while Windows still knows them.
    for nid, luid, profile, alias, ssid in con.execute(
            "SELECT id, luid, profile, alias, ssid FROM networks").fetchall():
        new_alias = adapter_alias(luid) or alias
        new_ssid = ssid
        if profile:
            if wifi is None:
                wifi = wifi_profile_names()
            new_ssid = wifi.get(profile) or ssid
        if (new_alias, new_ssid) != (alias, ssid):
            con.execute("UPDATE networks SET alias=?, ssid=? WHERE id=?", (new_alias, new_ssid, nid))
    after = con.execute("SELECT COUNT(*) FROM usage").fetchone()[0]
    return after - before


def cmd_collect(args):
    started = time.time()
    status = {"time": dt.datetime.now().isoformat(timespec="seconds"), "ok": False}
    con = open_db()
    try:
        if args.srum:
            src, method = args.srum, "file"
            rows = read_srum(src)
        else:
            tmp = tempfile.mkdtemp(prefix="netusage_")
            try:
                rows, problems = None, []
                for method in ("shadow copy", "service restart"):
                    try:
                        rows = read_srum(copy_srum(tmp, method))
                        break
                    except Exception as e:
                        problems.append("%s: %s: %s" % (method, type(e).__name__, e))
                        log("Could not read via %s - %s: %s" % (method, type(e).__name__, e))
                if rows is None:
                    raise RuntimeError(" | ".join(problems))
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
        added = import_rows(con, rows)
        status.update(ok=True, method=method, read=len(rows), added=added)
        if not meta_get(con, "first_collect"):
            meta_set(con, "first_collect", status["time"])
        log("Collected via %s: %d rows in Windows' log, %d new (%.1fs)"
            % (method, len(rows), added, time.time() - started))
    except Exception as e:
        status["error"] = "%s: %s" % (type(e).__name__, e)
        log("Collect FAILED - " + status["error"])
    meta_set(con, "last_collect", json.dumps(status))
    con.commit()
    con.close()
    try:
        build_report()
    except Exception as e:
        log("Dashboard build FAILED - %s: %s" % (type(e).__name__, e))
    return 0 if status["ok"] else 1


# --------------------------------------------------------------------------- friendly names

FRIENDLY_EXE = {
    "chrome.exe": "Google Chrome", "msedge.exe": "Microsoft Edge", "firefox.exe": "Firefox",
    "opera.exe": "Opera", "brave.exe": "Brave", "code.exe": "Visual Studio Code",
    "claude.exe": "Claude", "discord.exe": "Discord", "telegram.exe": "Telegram",
    "whatsapp.exe": "WhatsApp", "spotify.exe": "Spotify", "steam.exe": "Steam",
    "steamwebhelper.exe": "Steam (web helper)", "onedrive.exe": "OneDrive",
    "ms-teams.exe": "Microsoft Teams", "teams.exe": "Microsoft Teams", "zoom.exe": "Zoom",
    "python.exe": "Python", "pythonw.exe": "Python", "git-remote-https.exe": "Git",
    "svchost.exe": "Windows service host", "backgrounddownload.exe": "Background download (Windows)",
    "msmpeng.exe": "Microsoft Defender", "mpcmdrun.exe": "Microsoft Defender",
    "explorer.exe": "File Explorer", "searchapp.exe": "Windows Search",
    "searchhost.exe": "Windows Search", "msedgewebview2.exe": "Edge WebView2",
    "widgets.exe": "Widgets", "phoneexperiencehost.exe": "Phone Link",
    "googleupdate.exe": "Google Update", "updater.exe": "Updater",
    "ssh.exe": "SSH", "scp.exe": "SCP", "winscp.exe": "WinSCP", "node.exe": "Node.js",
    "idman.exe": "Internet Download Manager", "qbittorrent.exe": "qBittorrent",
    "utorrent.exe": "uTorrent", "vlc.exe": "VLC", "obs64.exe": "OBS Studio",
}
FRIENDLY_SVC = {
    "wuauserv": "Windows Update", "dosvc": "Delivery Optimization (Windows Update)",
    "bits": "Background Intelligent Transfer", "usosvc": "Update Orchestrator",
    "installservice": "Microsoft Store install service", "diagtrack": "Diagnostics & telemetry",
    "dnscache": "DNS client", "dhcp": "DHCP client", "windefend": "Microsoft Defender",
    "cryptsvc": "Cryptographic services", "wlidsvc": "Microsoft account sign-in",
    "wpnservice": "Push notifications", "w32time": "Windows Time", "licensemanager": "License manager",
    "clipsvc": "Client license service", "storsvc": "Storage service", "pushtoinstall": "Push to install",
    "wisvc": "Windows Insider service", "spooler": "Print spooler", "nlasvc": "Network location awareness",
    "cdpsvc": "Connected devices platform", "sense": "Defender for Endpoint", "mpssvc": "Windows Firewall",
    "wsearch": "Windows Search", "gamingservices": "Gaming Services", "xblauthmanager": "Xbox Live auth",
}
FRIENDLY_PKG = {
    "microsoft.windowsstore": "Microsoft Store", "microsoft.gamingservices": "Gaming Services",
    "microsoft.xboxapp": "Xbox", "microsoft.gamingapp": "Xbox", "microsoft.windows.photos": "Photos",
    "microsoft.windows.cortana": "Cortana", "microsoftwindows.client.webexperience": "Widgets",
    "microsoft.windows.contentdeliverymanager": "Windows tips & suggestions",
    "microsoft.zunevideo": "Movies & TV", "microsoft.zunemusic": "Media Player",
    "microsoft.bingweather": "Weather", "microsoft.bingnews": "News", "microsoft.outlookforwindows": "Outlook",
    "microsoftteams": "Microsoft Teams", "msteams": "Microsoft Teams", "spotifyab.spotifymusic": "Spotify",
    "5319275a.whatsappdesktop": "WhatsApp", "microsoft.yourphone": "Phone Link",
    "microsoft.windowsterminal": "Windows Terminal", "microsoft.copilot": "Copilot",
}


def describe_app(key):
    """-> (display name, detail line, kind)"""
    kind, _, val = key.partition(":")
    if kind == "exe":
        base = re.split(r"[\\/]", val)[-1] or val
        if val == "system":
            return "System (Windows kernel)", "System", "system"
        if val.startswith("system\\") or val.startswith("system/"):
            return "System: " + val[7:].title(), "System", "system"
        return FRIENDLY_EXE.get(base, base), base, "app"
    if kind == "svc":
        return FRIENDLY_SVC.get(val.lower(), val), "Windows service: " + val, "service"
    if kind == "pkg":
        name = val.split("_")[0]
        friendly = FRIENDLY_PKG.get(name.lower())
        if not friendly:
            friendly = name
            for p in ("MicrosoftWindows.", "Microsoft.Windows.", "Microsoft."):
                if friendly.startswith(p) and len(friendly) > len(p):
                    friendly = friendly[len(p):]
                    break
            friendly = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", friendly.split(".")[-1])
        return friendly, "Store app: " + name, "store"
    return "Unattributed system traffic", "Windows could not tie this to an app", "system"


IFTYPE_NAMES = {71: "Wi-Fi", 6: "Ethernet", 243: "Mobile broadband", 244: "Mobile broadband",
                24: "Loopback", 131: "Tunnel / VPN", 53: "Virtual adapter / VPN", 23: "PPP"}
VIRTUAL_ALIAS = re.compile(r"vEthernet|Hyper-V|VirtualBox|VMware|WSL|Loopback|TAP|WireGuard|"
                           r"OpenVPN|Tailscale|ZeroTier|Bluetooth|Local Area Connection\*|Wi-Fi Direct", re.I)


def describe_net(iftype, alias, ssid, profile):
    kind = IFTYPE_NAMES.get(iftype, "Unknown adapter" if not iftype else "Adapter type %d" % iftype)
    if ssid:
        label = ssid
    elif alias:
        label = alias
    elif profile:
        label = "%s network #%d" % (kind, profile)
    else:
        label = kind
    # Mobile hotspot / Wi-Fi Direct adapters are type 71 too, so check the name for every type.
    physical = iftype in (71, 6, 243, 244) and not (alias and VIRTUAL_ALIAS.search(alias))
    return label, kind, physical


# --------------------------------------------------------------------------- report

def build_report(open_after=False):
    if not os.path.exists(DB_PATH):
        raise SystemExit("No history yet - run 'collect' first.")
    con = open_db(readonly=True)
    apps, app_idx = [], {}
    for aid, key in con.execute("SELECT id, key FROM apps ORDER BY id"):
        name, detail, kind = describe_app(key)
        app_idx[aid] = len(apps)
        apps.append([name, detail, kind])
    nets, net_idx = [], {}
    for nid, luid, profile, iftype, alias, ssid in con.execute(
            "SELECT id, luid, profile, iftype, alias, ssid FROM networks ORDER BY id"):
        label, kind, physical = describe_net(iftype or 0, alias, ssid, profile)
        net_idx[nid] = len(nets)
        nets.append({"key": "%d:%d" % (luid, profile), "label": label, "kind": kind,
                     "alias": alias or "", "default": physical})
    # Windows writes one extra row per hour per network that has no app attached: the
    # network's TOTAL for that hour (it is what Settings > Data usage shows). Adding it to
    # the per-app rows would count everything twice. So, per hour and network:
    #   total = that row (or the sum of the app rows if Windows didn't write one),
    #   apps  = the per-app rows, plus "Other" for any small remainder no app was given.
    total_ids = {aid for aid, key in con.execute("SELECT id, key FROM apps") if key == TOTAL_KEY}
    other_idx = len(apps)
    apps.append(["Other (not tied to an app)", "Network total from Windows minus the apps listed", "system"])
    daily = {}
    def add(day, app, net, recv, sent):
        k = (day, app, net)
        v = daily.get(k)
        if v is None:
            daily[k] = [recv, sent]
        else:
            v[0] += recv
            v[1] += sent
    cur_key, total, parts = None, None, []
    def flush():
        if cur_key is None:
            return
        day, net = cur_key[1], cur_key[2]
        for a, r, s_ in parts:
            add(day, app_idx[a], net_idx[net], r, s_)
        if total is not None:
            rest_r = total[0] - sum(p[1] for p in parts)
            rest_s = total[1] - sum(p[2] for p in parts)
            if rest_r > 0 or rest_s > 0:
                add(day, other_idx, net_idx[net], max(0, rest_r), max(0, rest_s))
    for ts, d, n, a, recv, sent in con.execute(
            "SELECT ts, date(ts, 'unixepoch', 'localtime'), net, app, SUM(recv), SUM(sent) "
            "FROM usage GROUP BY ts, net, app ORDER BY ts, net"):
        if (ts, d, n) != cur_key:
            flush()
            cur_key, total, parts = (ts, d, n), None, []
        if a in total_ids:
            total = (recv, sent)
        else:
            parts.append((a, recv, sent))
    flush()
    days = sorted({k[0] for k in daily})
    day_idx = {d: i for i, d in enumerate(days)}
    rows = [[day_idx[d], a, n, v[0], v[1]] for (d, a, n), v in sorted(daily.items()) if v[0] or v[1]]
    span = con.execute("SELECT MIN(ts), MAX(ts) FROM usage").fetchone()
    last = meta_get(con, "last_collect")
    con.close()

    def iso(ts):
        return dt.datetime.fromtimestamp(ts).isoformat(timespec="minutes") if ts else None

    data = {
        "version": VERSION,
        "generated": dt.datetime.now().isoformat(timespec="minutes"),
        "firstRecord": iso(span[0]), "lastRecord": iso(span[1]),
        "lastCollect": json.loads(last) if last else None,
        "dataDir": DATA_DIR,
        "sourceUrl": SOURCE_URL,
        "days": days, "apps": apps, "nets": nets, "rows": rows,
    }
    with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
        html = f.read()
    payload = json.dumps(data, separators=(",", ":"), ensure_ascii=False).replace("</", "<\\/")
    html = html.replace("/*__DATA__*/null", payload)
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = DASH_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(html)
    os.replace(tmp, DASH_PATH)
    if open_after:
        open_dashboard()
    return DASH_PATH


def open_dashboard():
    if IS_WINDOWS:
        os.startfile(DASH_PATH)
    else:
        print(DASH_PATH)


def cmd_report(args):
    path = build_report(open_after=args.open)
    print("Dashboard written to", path)
    return 0


def fmt_bytes(b):
    for unit, size in (("TB", 1024 ** 4), ("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if b >= size:
            return "%.2f %s" % (b / size, unit)
    return "%d B" % b


def cmd_status(args):
    if not os.path.exists(DB_PATH):
        print("Nothing recorded yet.")
        return 1
    con = open_db(readonly=True)
    print("History file:", DB_PATH)
    print("Last collect:", meta_get(con, "last_collect"))
    print()
    print("%-9s %12s %12s %12s" % ("Month", "Download", "Upload", "Total"))
    # Per hour+network: Windows' own total row if there is one, else the sum of the app rows.
    for m, recv, sent in con.execute(
            "SELECT strftime('%Y-%m', ts, 'unixepoch', 'localtime') m, SUM(r), SUM(s) FROM ("
            " SELECT u.ts, u.net,"
            "  CASE WHEN SUM(a.key = ?) > 0 THEN SUM(CASE WHEN a.key = ? THEN u.recv ELSE 0 END) ELSE SUM(u.recv) END r,"
            "  CASE WHEN SUM(a.key = ?) > 0 THEN SUM(CASE WHEN a.key = ? THEN u.sent ELSE 0 END) ELSE SUM(u.sent) END s"
            " FROM usage u JOIN apps a ON a.id = u.app GROUP BY u.ts, u.net"
            ") GROUP BY m ORDER BY m", (TOTAL_KEY,) * 4):
        print("%-9s %12s %12s %12s" % (m, fmt_bytes(recv), fmt_bytes(sent), fmt_bytes(recv + sent)))
    print("\n(All adapters, including virtual ones. The dashboard lets you exclude VPN/virtual adapters.)")
    con.close()
    return 0


# --------------------------------------------------------------------------- install / uninstall

def is_admin():
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def desktop_dir():
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(260)
        if ctypes.windll.shell32.SHGetFolderPathW(None, 0x10, None, 0, buf) == 0:   # CSIDL_DESKTOPDIRECTORY
            return buf.value
    except Exception:
        pass
    return os.path.join(os.path.expanduser("~"), "Desktop")


def task_xml(pythonw, script):
    start = dt.datetime.now().replace(minute=0, second=0, microsecond=0) + dt.timedelta(hours=1)
    esc = lambda s: s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    return """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>NetUsage: copies Windows' hourly network usage log into a permanent monthly history and refreshes the Data Usage dashboard.</Description>
  </RegistrationInfo>
  <Triggers>
    <TimeTrigger>
      <Repetition><Interval>PT1H</Interval><StopAtDurationEnd>false</StopAtDurationEnd></Repetition>
      <StartBoundary>%s</StartBoundary>
      <Enabled>true</Enabled>
    </TimeTrigger>
    <BootTrigger><Enabled>true</Enabled><Delay>PT10M</Delay></BootTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author"><UserId>S-1-5-18</UserId><RunLevel>HighestAvailable</RunLevel></Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings><StopOnIdleEnd>false</StopOnIdleEnd><RestartOnIdle>false</RestartOnIdle></IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <ExecutionTimeLimit>PT20M</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>%s</Command>
      <Arguments>"%s" collect</Arguments>
    </Exec>
  </Actions>
</Task>
""" % (start.strftime("%Y-%m-%dT%H:%M:%S"), esc(pythonw), esc(script))


def cmd_install(args):
    if not IS_WINDOWS:
        raise SystemExit("Install only works on Windows.")
    if not is_admin():
        raise SystemExit("Please run Install.bat (it asks for administrator rights).")
    if sys.version_info < (3, 10):
        raise SystemExit("Python 3.10 or newer is needed (this is %s)." % sys.version.split()[0])
    exe = sys.executable
    if "WindowsApps" in exe:
        raise SystemExit("This Python comes from the Microsoft Store, which background tasks "
                         "cannot use.\nInstall Python from https://www.python.org/downloads/ "
                         "and run Install.bat again.")
    pythonw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if not os.path.exists(pythonw):
        pythonw = exe

    print("1/5  Copying NetUsage to", APP_DIR)
    os.makedirs(DATA_DIR, exist_ok=True)
    if os.path.normcase(HERE) != os.path.normcase(APP_DIR):
        os.makedirs(APP_DIR, exist_ok=True)
        for name in ("netusage.py", "dashboard_template.html", "LICENSE"):
            if os.path.exists(os.path.join(HERE, name)):
                shutil.copy2(os.path.join(HERE, name), os.path.join(APP_DIR, name))
        shutil.copytree(os.path.join(HERE, "lib"), os.path.join(APP_DIR, "lib"), dirs_exist_ok=True)
    # Only SYSTEM and administrators may change the files the SYSTEM task runs; users can read.
    run(["icacls", DATA_DIR, "/inheritance:r",
         "/grant:r", "*S-1-5-18:(OI)(CI)F", "*S-1-5-32-544:(OI)(CI)F", "*S-1-5-32-545:(OI)(CI)RX"])

    print("2/5  Reading Windows' usage log for the first time (can take a minute)...")
    class A: srum = None
    code = cmd_collect(A())
    if code != 0:
        print("     The first read failed - see", LOG_PATH)

    print("3/5  Creating the hourly background task '%s'" % TASK_NAME)
    xml_path = os.path.join(DATA_DIR, "task.xml")
    with open(xml_path, "w", encoding="utf-16") as f:
        f.write(task_xml(pythonw, os.path.join(APP_DIR, "netusage.py")))
    r = run(["schtasks", "/create", "/tn", TASK_NAME, "/xml", xml_path, "/f"])
    os.remove(xml_path)
    if r.returncode != 0:
        raise SystemExit("Could not create the task:\n" + r.stdout + r.stderr)

    print("4/5  Putting a 'Data Usage' shortcut on your desktop")
    url = "file:///" + DASH_PATH.replace("\\", "/")
    with open(os.path.join(desktop_dir(), SHORTCUT_NAME), "w", encoding="utf-8") as f:
        f.write("[InternetShortcut]\nURL=%s\nIconFile=%s\nIconIndex=0\n"
                % (url, os.path.join(_SYS32, "netcenter.dll")))

    print("5/5  Done.\n")
    cmd_status(None)
    print("\nOpening the dashboard...")
    open_dashboard()
    return 0


def cmd_uninstall(args):
    run(["schtasks", "/delete", "/tn", TASK_NAME, "/f"])
    try:
        os.remove(os.path.join(desktop_dir(), SHORTCUT_NAME))
    except OSError:
        pass
    print("Background task and desktop shortcut removed.")
    if args.purge:
        shutil.rmtree(DATA_DIR, ignore_errors=True)
        print("All recorded history deleted.")
    else:
        print("Your recorded history is kept in", DATA_DIR)
    return 0


def main():
    p = argparse.ArgumentParser(
        description="NetUsage %s - monthly download/upload history" % VERSION,
        epilog="Copyright (C) 2026 Sakura. Free software under the GNU AGPL v3 or later; "
               "it comes with ABSOLUTELY NO WARRANTY. Source: %s" % SOURCE_URL)
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("collect")
    c.add_argument("--srum", help="read this SRUDB.dat copy instead of the live one")
    r = sub.add_parser("report")
    r.add_argument("--open", action="store_true")
    sub.add_parser("status")
    sub.add_parser("install")
    u = sub.add_parser("uninstall")
    u.add_argument("--purge", action="store_true")
    args = p.parse_args()
    return {"collect": cmd_collect, "report": cmd_report, "status": cmd_status,
            "install": cmd_install, "uninstall": cmd_uninstall}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
