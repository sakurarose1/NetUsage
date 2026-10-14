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

It can also warn you (Windows notifications) when an app suddenly uses far
more data than usual, when you get close to a monthly data limit you set, and
when a new version of NetUsage is published.

Commands
--------
    python netusage.py install            (as administrator) set everything up
    python netusage.py uninstall [--purge]  remove the tasks (and the data with --purge)
    python netusage.py collect            import new rows + rebuild the dashboard
    python netusage.py report [--open]    rebuild the dashboard only
    python netusage.py status             show what has been recorded so far
    python netusage.py settings           open the settings window
    python netusage.py notify [--test]    show any new alerts as Windows notifications

Files live in C:\\ProgramData\\NetUsage  (override with the NETUSAGE_DATA env var).
"""

import sys
import os
import struct

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "lib"))

import argparse
import base64
import datetime as dt
import json
import re
import shutil
import sqlite3
import statistics
import subprocess
import tempfile
import time

VERSION = "1.2"
SOURCE_URL = "https://github.com/sakurarose1/NetUsage"
RELEASES_API = "https://api.github.com/repos/sakurarose1/NetUsage/releases/latest"
RELEASES_PAGE = SOURCE_URL + "/releases/latest"
IS_WINDOWS = os.name == "nt"

DATA_DIR = os.environ.get("NETUSAGE_DATA") or os.path.join(
    os.environ.get("ProgramData", r"C:\ProgramData"), "NetUsage")
DB_PATH = os.path.join(DATA_DIR, "history.sqlite")
DASH_PATH = os.path.join(DATA_DIR, "dashboard.html")
LOG_PATH = os.path.join(DATA_DIR, "collector.log")
SETTINGS_PATH = os.path.join(DATA_DIR, "settings.json")
ALERTS_PATH = os.path.join(DATA_DIR, "alerts.json")
APP_DIR = os.path.join(DATA_DIR, "app")
TEMPLATE_PATH = os.path.join(HERE, "dashboard_template.html")
DASH_URL = "file:///" + DASH_PATH.replace("\\", "/")
GB = 1024 ** 3

_WIN = os.environ.get("SystemRoot", r"C:\Windows")
# 32-bit Python on 64-bit Windows sees SysWOW64 instead of System32; "Sysnative" is the way through.
_SYS32 = os.path.join(_WIN, "Sysnative") if (IS_WINDOWS and struct.calcsize("P") == 4
                                              and os.path.isdir(os.path.join(_WIN, "Sysnative"))) else os.path.join(_WIN, "System32")
SRUM_PATH = os.path.join(_SYS32, "sru", "SRUDB.dat")
ESENTUTL = os.path.join(_SYS32, "esentutl.exe")
NET_TABLE = "{973F5D5C-1D90-4944-BE8E-24B94231A174}"   # SRUM "Network Data Usage"
TASK_NAME = "NetUsage Collector"
NOTIFY_TASK_NAME = "NetUsage Notifier"
SHORTCUT_NAME = "Data Usage.url"
SETTINGS_SHORTCUT = "NetUsage Settings.lnk"
START_MENU_DIR = os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"),
                              "Microsoft", "Windows", "Start Menu", "Programs", "NetUsage")
POWERSHELL = os.path.join(_SYS32, "WindowsPowerShell", "v1.0", "powershell.exe")
AUMID = "SakuraRose1.NetUsage"            # how NetUsage's notifications are labelled in Windows
FALLBACK_AUMID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"

SPIKE_FACTOR = 3            # an app must use 3x its usual daily amount ...
SPIKE_BASELINE_DAYS = 30    # ... measured as the median of its previous 30 days
ALERT_KEEP_DAYS = 7         # alerts.json keeps a week of alerts
ALERT_SHOW_HOURS = 24       # the notifier only pops up alerts younger than this

DEFAULT_SETTINGS = {
    "notifications": True,      # show Windows notifications at all
    "update_check": True,       # ask GitHub once a day whether a newer version exists
    "spike_alerts": True,       # warn when an app uses far more than usual in a day
    "spike_min_gb": 2.0,        # ... but only if it is at least this much
    "limit_enabled": False,     # monthly data limit (off unless the user turns it on)
    "limit_gb": 100.0,
    "limit_count": "both",      # "both" = download + upload, "download" = download only
    "limit_warn_percent": 80,
    "cycle_start_day": 1,       # the day the monthly limit resets
}

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


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


# --------------------------------------------------------------------------- settings

def clean_settings(raw):
    """Fill in defaults and clamp every value, so a hand-edited file can't break anything."""
    out = dict(DEFAULT_SETTINGS)
    if not isinstance(raw, dict):
        return out
    for k in ("notifications", "update_check", "spike_alerts", "limit_enabled"):
        if isinstance(raw.get(k), bool):
            out[k] = raw[k]

    def num(k, lo, hi, cast):
        try:
            v = cast(raw.get(k, out[k]))
        except (TypeError, ValueError):
            return out[k]
        if v != v:                       # NaN
            return out[k]
        return min(hi, max(lo, v))
    out["spike_min_gb"] = num("spike_min_gb", 0.1, 1000.0, float)
    out["limit_gb"] = num("limit_gb", 1.0, 1000000.0, float)
    out["limit_warn_percent"] = num("limit_warn_percent", 10, 99, int)
    out["cycle_start_day"] = num("cycle_start_day", 1, 28, int)
    out["limit_count"] = "download" if raw.get("limit_count") == "download" else "both"
    return out


def load_settings():
    return clean_settings(read_json(SETTINGS_PATH, {}))


def save_settings(s):
    write_json(SETTINGS_PATH, clean_settings(s))


# --------------------------------------------------------------------------- update check

def parse_version(v):
    nums = [int(x) for x in re.findall(r"\d+", str(v))[:4]]
    return tuple(nums + [0] * (4 - len(nums)))


def is_newer(v):
    return bool(v) and parse_version(v) > parse_version(VERSION)


def check_for_update(con, settings, force=False):
    """Ask GitHub for the latest release, at most about once a day. Returns the cached
    result {version, url, published, checked} or None. Never raises."""
    cached = json.loads(meta_get(con, "latest_release") or "null")
    if not settings["update_check"] and not force:
        return cached
    if cached and not force and time.time() - cached.get("checked", 0) < 20 * 3600:
        return cached
    import urllib.request
    req = urllib.request.Request(RELEASES_API, headers={
        "User-Agent": "NetUsage/" + VERSION, "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            j = json.load(r)
        info = {"version": str(j.get("tag_name", "")).lstrip("vV"),
                "url": j.get("html_url") or RELEASES_PAGE,
                "published": j.get("published_at"),
                "checked": int(time.time())}
        if not info["version"]:
            raise ValueError("no tag_name in GitHub's answer")
    except Exception as e:
        log("Update check failed - %s: %s" % (type(e).__name__, e))
        if cached:      # don't retry every hour while offline: wait a few hours
            cached["checked"] = int(time.time()) - 16 * 3600
            meta_set(con, "latest_release", json.dumps(cached))
            return cached
        meta_set(con, "latest_release", json.dumps({"version": "", "checked": int(time.time()) - 16 * 3600}))
        return None
    meta_set(con, "latest_release", json.dumps(info))
    if is_newer(info["version"]):
        log("Update available: NetUsage %s (you have %s)" % (info["version"], VERSION))
    return info


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
    if not getattr(args, "srum", None):
        check_for_update(con, load_settings())
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

def compute_daily(con):
    """Turn the hourly history into per-day rows [day, app, network, download, upload]."""
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
    return {"apps": apps, "nets": nets, "days": days, "rows": rows, "other": other_idx}


# --------------------------------------------------------------------------- alerts

def cycle_for(day, start_day):
    """The monthly period (first day, last day) that contains `day`."""
    y, m = day.year, day.month
    if day.day < start_day:
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
    return dt.date(y, m, start_day), dt.date(ny, nm, start_day) - dt.timedelta(days=1)


def fmt_gb(b):
    g = b / GB
    return "%.1f GB" % g if g >= 1 else "%.0f MB" % (b / 1024 ** 2)


def nice_date(d):
    return "%d %s" % (d.day, d.strftime("%b"))


def compute_alerts(d, settings, latest, today=None):
    """Alerts that are true right now. Each has a stable id, so the same alert is only
    ever shown once. Only "real" networks count (VPN / virtual adapters are left out,
    the same as the dashboard's default)."""
    today = today or dt.date.today()
    out = []
    physical = {i for i, n in enumerate(d["nets"]) if n["default"]}
    days = d["days"]

    if settings["spike_alerts"] and days:
        first = dt.date.fromisoformat(days[0])
        per_app = {}
        for di, a, n, recv, sent in d["rows"]:
            if n in physical and a != d["other"]:
                byday = per_app.setdefault(d["apps"][a][0], {})
                byday[days[di]] = byday.get(days[di], 0) + recv + sent
        floor = settings["spike_min_gb"] * GB
        for name, byday in per_app.items():
            for day in (today - dt.timedelta(days=1), today):
                used = byday.get(day.isoformat(), 0)
                if used < floor:
                    continue
                prev = [byday.get((day - dt.timedelta(days=k)).isoformat(), 0)
                        for k in range(1, SPIKE_BASELINE_DAYS + 1)
                        if day - dt.timedelta(days=k) >= first]
                if len(prev) < 7:          # not enough history yet to know what "usual" is
                    continue
                usual = statistics.median(prev)
                threshold = max(floor, SPIKE_FACTOR * usual)
                if used < threshold or prev[0] >= threshold:
                    continue               # normal, or the same spike already alerted yesterday
                when = "today" if day == today else "yesterday"
                body = ("That's much more than usual (about %s a day)." % fmt_gb(usual)
                        if usual >= 1024 ** 2 else "It doesn't usually use this much.")
                out.append({"id": "spike:%s:%s" % (name, day.isoformat()), "kind": "spike",
                            "title": "%s used %s %s" % (name, fmt_gb(used), when),
                            "body": body})

    if settings["limit_enabled"]:
        start, end = cycle_for(today, settings["cycle_start_day"])
        used = 0
        for di, a, n, recv, sent in d["rows"]:
            if n in physical and start.isoformat() <= days[di] <= end.isoformat():
                used += recv if settings["limit_count"] == "download" else recv + sent
        limit = settings["limit_gb"] * GB
        pct = 100.0 * used / limit
        level = 100 if pct >= 100 else settings["limit_warn_percent"] if pct >= settings["limit_warn_percent"] else None
        if level:
            resets = nice_date(end + dt.timedelta(days=1))
            if level == 100:
                title = "You've reached your %s data limit" % fmt_gb(limit)
            else:
                title = "You've used %d%% of your %s data limit" % (min(99, round(pct)), fmt_gb(limit))
            out.append({"id": "limit:%s:%d" % (start.isoformat(), level), "kind": "limit",
                        "title": title,
                        "body": "%s used so far. The limit resets on %s." % (fmt_gb(used), resets)})

    if settings["update_check"] and latest and is_newer(latest.get("version")):
        out.append({"id": "update:%s" % latest["version"], "kind": "update",
                    "title": "NetUsage %s is available" % latest["version"],
                    "body": "You have %s. Click to open the download page." % VERSION,
                    "url": latest.get("url") or RELEASES_PAGE})
    return out


def record_alerts(current):
    """Merge the alerts that are true now into alerts.json, keeping when each first appeared."""
    now = dt.datetime.now()
    old = read_json(ALERTS_PATH, {}) or {}
    keep = {}
    for a in old.get("alerts", []) if isinstance(old, dict) else []:
        try:
            if now - dt.datetime.fromisoformat(a["time"]) < dt.timedelta(days=ALERT_KEEP_DAYS):
                keep[a["id"]] = a
        except (KeyError, TypeError, ValueError):
            pass
    for a in current:
        if a["id"] in keep:
            keep[a["id"]].update({k: v for k, v in a.items() if k != "time"})
        else:
            a = dict(a, time=now.isoformat(timespec="seconds"))
            keep[a["id"]] = a
            log("Alert: %s - %s" % (a["title"], a["body"]))
    alerts = sorted(keep.values(), key=lambda a: a["time"], reverse=True)
    try:
        write_json(ALERTS_PATH, {"updated": now.isoformat(timespec="seconds"), "alerts": alerts})
    except OSError as e:
        log("Could not write alerts.json - %s" % e)
    return alerts


# --------------------------------------------------------------------------- report

def build_report(open_after=False):
    if not os.path.exists(DB_PATH):
        raise SystemExit("No history yet - run 'collect' first.")
    settings = load_settings()
    con = open_db(readonly=True)
    d = compute_daily(con)
    span = con.execute("SELECT MIN(ts), MAX(ts) FROM usage").fetchone()
    last = meta_get(con, "last_collect")
    latest = json.loads(meta_get(con, "latest_release") or "null")
    con.close()
    alerts = record_alerts(compute_alerts(d, settings, latest))

    def iso(ts):
        return dt.datetime.fromtimestamp(ts).isoformat(timespec="minutes") if ts else None

    data = {
        "version": VERSION,
        "generated": dt.datetime.now().isoformat(timespec="minutes"),
        "firstRecord": iso(span[0]), "lastRecord": iso(span[1]),
        "lastCollect": json.loads(last) if last else None,
        "dataDir": DATA_DIR,
        "sourceUrl": SOURCE_URL,
        "settings": settings,
        "update": latest if latest and settings["update_check"] and is_newer(latest.get("version")) else None,
        "updateChecked": latest.get("checked") if latest else None,
        "alerts": [a for a in alerts if a.get("kind") != "update"][:10],
        "days": d["days"], "apps": d["apps"], "nets": d["nets"], "rows": d["rows"],
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


# --------------------------------------------------------------------------- notifications

def _xml_escape(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&apos;"))


def register_aumid():
    """Give NetUsage's notifications their own name and icon (per user, no admin needed)."""
    if not IS_WINDOWS:
        return False
    try:
        import winreg
        k = winreg.CreateKey(winreg.HKEY_CURRENT_USER, "Software\\Classes\\AppUserModelId\\" + AUMID)
        winreg.SetValueEx(k, "DisplayName", 0, winreg.REG_SZ, "NetUsage")
        winreg.SetValueEx(k, "IconUri", 0, winreg.REG_SZ, os.path.join(_SYS32, "netcenter.dll"))
        winreg.CloseKey(k)
        return True
    except OSError:
        return False


def show_toast(title, body, url=None):
    """Show a Windows notification. Clicking it opens `url` (the dashboard by default)."""
    if not IS_WINDOWS:
        print("[notification] %s - %s" % (title, body))
        return True
    xml = ('<toast activationType="protocol" launch="%s"><visual><binding template="ToastGeneric">'
           '<text>%s</text><text>%s</text></binding></visual></toast>'
           % (_xml_escape(url or DASH_URL), _xml_escape(title), _xml_escape(body)))
    for aumid in ((AUMID, FALLBACK_AUMID) if register_aumid() else (FALLBACK_AUMID,)):
        ps = ("$ErrorActionPreference = 'Stop';"
              "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null;"
              "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null;"
              "$x = New-Object Windows.Data.Xml.Dom.XmlDocument; $x.LoadXml('%s');"
              "$t = New-Object Windows.UI.Notifications.ToastNotification $x;"
              "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('%s').Show($t)"
              % (xml.replace("'", "''"), aumid.replace("'", "''")))
        enc = base64.b64encode(ps.encode("utf-16-le")).decode("ascii")
        try:
            r = run([POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                     "-EncodedCommand", enc], timeout=60)
        except Exception as e:
            log("Notification failed - %s: %s" % (type(e).__name__, e))
            continue
        if r.returncode == 0:
            return True
        log("Notification via %s failed: %s" % (aumid, (r.stderr or r.stdout).strip()[-300:]))
    return False


def seen_path():
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "NetUsage", "seen.json")


def cmd_notify(args):
    """Runs as the signed-in user (hourly task + at sign-in): pops up alerts not shown yet."""
    if getattr(args, "test", False):
        ok = show_toast("NetUsage notifications are working",
                        "This is how alerts will look. Click to open your Data Usage dashboard.")
        print("Test notification sent." if ok else "The notification could not be shown - see collector.log.")
        return 0 if ok else 1
    settings = load_settings()
    if not settings["notifications"]:
        return 0
    alerts = (read_json(ALERTS_PATH, {}) or {}).get("alerts", [])
    path = seen_path()
    seen = read_json(path, None)
    first_run = not isinstance(seen, dict)
    seen = seen if isinstance(seen, dict) else {}
    now = dt.datetime.now()
    shown = 0
    for a in sorted(alerts, key=lambda a: a.get("time", "")):
        aid = a.get("id")
        if not aid or aid in seen:
            continue
        try:
            age = now - dt.datetime.fromisoformat(a["time"])
        except (KeyError, TypeError, ValueError):
            continue
        seen[aid] = now.isoformat(timespec="seconds")
        if first_run and age > dt.timedelta(hours=2):
            continue              # don't replay old alerts the very first time
        if age > dt.timedelta(hours=ALERT_SHOW_HOURS) or shown >= 3:
            continue
        if show_toast(a.get("title", "NetUsage"), a.get("body", ""), a.get("url")):
            shown += 1
    cutoff = (now - dt.timedelta(days=ALERT_KEEP_DAYS + 7)).isoformat()
    seen = {k: v for k, v in seen.items() if v >= cutoff}
    try:
        write_json(path, seen)
    except OSError:
        pass
    return 0


# --------------------------------------------------------------------------- settings window

def relaunch_as_admin(arguments):
    import ctypes
    exe = sys.executable
    pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.exists(pyw):
        exe = pyw
    params = '"%s" %s' % (os.path.abspath(__file__), arguments)
    return ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, 1) > 32


def cmd_settings(args):
    if IS_WINDOWS and not is_admin():
        # settings.json sits next to the history, which only administrators may change
        if relaunch_as_admin("settings"):
            return 0
    try:
        import tkinter as tk
        from tkinter import ttk, messagebox
    except ImportError:
        print("The settings window needs Tkinter, which is part of the standard Python "
              "installer from python.org. Edit %s by hand instead." % SETTINGS_PATH)
        return 1

    s = load_settings()
    root = tk.Tk()
    root.title("NetUsage Settings")
    root.resizable(False, False)
    try:
        root.iconbitmap(default="")
    except Exception:
        pass
    pad = {"padx": 14, "pady": 4}
    frm = ttk.Frame(root, padding=(6, 10, 6, 6))
    frm.grid(sticky="nsew")

    v_notify = tk.BooleanVar(value=s["notifications"])
    v_update = tk.BooleanVar(value=s["update_check"])
    v_spike = tk.BooleanVar(value=s["spike_alerts"])
    v_spike_gb = tk.StringVar(value=("%g" % s["spike_min_gb"]))
    v_limit = tk.BooleanVar(value=s["limit_enabled"])
    v_limit_gb = tk.StringVar(value=("%g" % s["limit_gb"]))
    v_count = tk.StringVar(value="Download + upload" if s["limit_count"] == "both" else "Download only")
    v_warn = tk.StringVar(value=str(s["limit_warn_percent"]))
    v_day = tk.StringVar(value=str(s["cycle_start_day"]))
    status = tk.StringVar(value="")

    def heading(row, text):
        ttk.Label(frm, text=text, font=("Segoe UI", 10, "bold")).grid(row=row, column=0, columnspan=4, sticky="w", padx=14, pady=(12, 2))

    def note(row, text):
        ttk.Label(frm, text=text, foreground="#666", wraplength=420, justify="left").grid(
            row=row, column=0, columnspan=4, sticky="w", padx=34, pady=(0, 4))

    heading(0, "Notifications")
    ttk.Checkbutton(frm, text="Show Windows notifications for the alerts below", variable=v_notify).grid(row=1, column=0, columnspan=4, sticky="w", **pad)

    heading(2, "Spike alerts")
    ttk.Checkbutton(frm, text="Tell me when an app uses far more data than usual", variable=v_spike).grid(row=3, column=0, columnspan=4, sticky="w", **pad)
    sp = ttk.Frame(frm)
    sp.grid(row=4, column=0, columnspan=4, sticky="w", padx=34)
    ttk.Label(sp, text="Only when it uses at least").pack(side="left")
    e_spike = ttk.Spinbox(sp, from_=0.5, to=100, increment=0.5, width=6, textvariable=v_spike_gb)
    e_spike.pack(side="left", padx=6)
    ttk.Label(sp, text="GB in one day").pack(side="left")
    note(5, "An alert fires when an app's usage today is at least this much AND more than "
            "3 times what it normally uses in a day - so your usual heavy apps won't keep alerting.")

    heading(6, "Monthly data limit")
    ttk.Checkbutton(frm, text="I have a monthly data limit", variable=v_limit).grid(row=7, column=0, columnspan=4, sticky="w", **pad)
    lim = ttk.Frame(frm)
    lim.grid(row=8, column=0, columnspan=4, sticky="w", padx=34)
    ttk.Label(lim, text="Limit").grid(row=0, column=0, sticky="w", pady=3)
    e_gb = ttk.Spinbox(lim, from_=1, to=100000, increment=10, width=8, textvariable=v_limit_gb)
    e_gb.grid(row=0, column=1, sticky="w", padx=6)
    ttk.Label(lim, text="GB").grid(row=0, column=2, sticky="w")
    ttk.Label(lim, text="Counts").grid(row=1, column=0, sticky="w", pady=3)
    e_count = ttk.Combobox(lim, textvariable=v_count, state="readonly", width=18,
                           values=["Download + upload", "Download only"])
    e_count.grid(row=1, column=1, columnspan=2, sticky="w", padx=6)
    ttk.Label(lim, text="Resets on day").grid(row=2, column=0, sticky="w", pady=3)
    e_day = ttk.Spinbox(lim, from_=1, to=28, increment=1, width=5, textvariable=v_day)
    e_day.grid(row=2, column=1, sticky="w", padx=6)
    ttk.Label(lim, text="of each month").grid(row=2, column=2, sticky="w")
    ttk.Label(lim, text="Warn me at").grid(row=3, column=0, sticky="w", pady=3)
    e_warn = ttk.Spinbox(lim, from_=10, to=99, increment=5, width=5, textvariable=v_warn)
    e_warn.grid(row=3, column=1, sticky="w", padx=6)
    ttk.Label(lim, text="% and again at 100%").grid(row=3, column=2, sticky="w")

    heading(9, "Updates")
    ttk.Checkbutton(frm, text="Check for new versions of NetUsage once a day", variable=v_update).grid(row=10, column=0, columnspan=4, sticky="w", **pad)
    note(11, "Asks github.com/sakurarose1/NetUsage for the latest release. Nothing about your usage is sent.")

    def sync_state(*_):
        e_spike.state(["!disabled"] if v_spike.get() else ["disabled"])
        for w in (e_gb, e_day, e_warn):
            w.state(["!disabled"] if v_limit.get() else ["disabled"])
        e_count.state(["!disabled", "readonly"] if v_limit.get() else ["disabled"])
    v_spike.trace_add("write", sync_state)
    v_limit.trace_add("write", sync_state)
    sync_state()

    def collect_values():
        def f(var, what):
            try:
                return float(var.get().replace(",", "."))
            except ValueError:
                raise ValueError("Please enter a number for %s." % what)
        return clean_settings({
            "notifications": v_notify.get(), "update_check": v_update.get(),
            "spike_alerts": v_spike.get(), "spike_min_gb": f(v_spike_gb, "the spike alert size"),
            "limit_enabled": v_limit.get(), "limit_gb": f(v_limit_gb, "the data limit"),
            "limit_count": "both" if v_count.get().startswith("Download +") else "download",
            "limit_warn_percent": int(f(v_warn, "the warning level")),
            "cycle_start_day": int(f(v_day, "the reset day")),
        })

    def save():
        try:
            new = collect_values()
        except ValueError as e:
            messagebox.showerror("NetUsage Settings", str(e), parent=root)
            return False
        try:
            save_settings(new)
        except OSError as e:
            messagebox.showerror("NetUsage Settings", "Could not save the settings:\n%s" % e, parent=root)
            return False
        try:
            if new["update_check"]:
                con = open_db()
                check_for_update(con, new)
                con.commit()
                con.close()
            build_report()
            if new["notifications"]:
                cmd_notify(argparse.Namespace(test=False))
        except Exception as e:
            log("Rebuilding after saving settings failed - %s: %s" % (type(e).__name__, e))
        status.set("Saved. The dashboard has been updated.")
        return True

    def test():
        status.set("Sending a test notification...")
        root.update_idletasks()
        ok = show_toast("NetUsage notifications are working",
                        "This is how alerts will look. Click to open your Data Usage dashboard.")
        status.set("Test notification sent - check the corner of your screen." if ok
                   else "The notification could not be shown. Details are in collector.log.")

    def check_now():
        status.set("Checking github.com for a newer version...")
        root.update_idletasks()
        try:
            con = open_db()
            info = check_for_update(con, load_settings(), force=True)
            con.commit()
            con.close()
        except Exception as e:
            info = None
            log("Update check failed - %s" % e)
        if not info or not info.get("version"):
            status.set("Could not reach GitHub. Check your internet connection.")
        elif is_newer(info["version"]):
            if messagebox.askyesno("NetUsage", "NetUsage %s is available (you have %s).\n\nOpen the download page?"
                                   % (info["version"], VERSION), parent=root):
                import webbrowser
                webbrowser.open(info.get("url") or RELEASES_PAGE)
            status.set("NetUsage %s is available." % info["version"])
        else:
            status.set("You have the latest version (%s)." % VERSION)

    btns = ttk.Frame(frm)
    btns.grid(row=12, column=0, columnspan=4, sticky="ew", padx=14, pady=(14, 4))
    ttk.Button(btns, text="Send test notification", command=test).pack(side="left")
    ttk.Button(btns, text="Check for updates now", command=check_now).pack(side="left", padx=6)
    ttk.Button(btns, text="Save and close", command=lambda: save() and root.after(400, root.destroy)).pack(side="right")
    ttk.Button(btns, text="Save", command=save).pack(side="right", padx=6)
    ttk.Label(frm, textvariable=status, foreground="#2a78d6").grid(row=13, column=0, columnspan=4, sticky="w", padx=14, pady=(2, 6))
    ttk.Label(frm, text="NetUsage %s · free software (GNU AGPL v3 or later)" % VERSION, foreground="#888").grid(
        row=14, column=0, columnspan=4, sticky="w", padx=14, pady=(0, 4))
    root.mainloop()
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


def _xml(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def task_xml(pythonw, script):
    """Hourly collector, runs as SYSTEM (needed to read Windows' usage database)."""
    start = dt.datetime.now().replace(minute=0, second=0, microsecond=0) + dt.timedelta(hours=1)
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
""" % (start.strftime("%Y-%m-%dT%H:%M:%S"), _xml(pythonw), _xml(script))


def notify_task_xml(pythonw, script):
    """Hourly (a few minutes after the collector) and at sign-in, as whoever is signed in:
    notifications can only be shown inside the user's own session, not by SYSTEM."""
    start = dt.datetime.now().replace(minute=8, second=0, microsecond=0) + dt.timedelta(hours=1)
    return """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>NetUsage: shows NetUsage alerts (usage spikes, data limit, new versions) as Windows notifications.</Description>
  </RegistrationInfo>
  <Triggers>
    <TimeTrigger>
      <Repetition><Interval>PT1H</Interval><StopAtDurationEnd>false</StopAtDurationEnd></Repetition>
      <StartBoundary>%s</StartBoundary>
      <Enabled>true</Enabled>
    </TimeTrigger>
    <LogonTrigger><Enabled>true</Enabled><Delay>PT2M</Delay></LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author"><GroupId>S-1-5-32-545</GroupId><RunLevel>LeastPrivilege</RunLevel></Principal>
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
    <ExecutionTimeLimit>PT5M</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>%s</Command>
      <Arguments>"%s" notify</Arguments>
    </Exec>
  </Actions>
</Task>
""" % (start.strftime("%Y-%m-%dT%H:%M:%S"), _xml(pythonw), _xml(script))


def create_task(name, xml):
    xml_path = os.path.join(DATA_DIR, "task.xml")
    with open(xml_path, "w", encoding="utf-16") as f:
        f.write(xml)
    try:
        r = run(["schtasks", "/create", "/tn", name, "/xml", xml_path, "/f"])
    finally:
        os.remove(xml_path)
    if r.returncode != 0:
        raise SystemExit("Could not create the task '%s':\n%s%s" % (name, r.stdout, r.stderr))


def make_lnk(path, target, arguments, description, icon):
    """Create a Windows .lnk shortcut (via the built-in WScript.Shell COM object)."""
    q = lambda v: "'" + v.replace("'", "''") + "'"
    ps = ("$s = (New-Object -ComObject WScript.Shell).CreateShortcut(%s);"
          "$s.TargetPath = %s; $s.Arguments = %s; $s.Description = %s; $s.IconLocation = %s;"
          "$s.WorkingDirectory = %s; $s.Save()"
          % (q(path), q(target), q(arguments), q(description), q(icon), q(os.path.dirname(target))))
    enc = base64.b64encode(ps.encode("utf-16-le")).decode("ascii")
    r = run([POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", enc])
    if r.returncode != 0:
        print("     (could not create %s: %s)" % (os.path.basename(path), (r.stderr or "").strip()[-200:]))


def write_url_shortcut(path):
    with open(path, "w", encoding="utf-8") as f:
        f.write("[InternetShortcut]\nURL=%s\nIconFile=%s\nIconIndex=0\n"
                % (DASH_URL, os.path.join(_SYS32, "netcenter.dll")))


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

    print("1/6  Copying NetUsage to", APP_DIR)
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
    save_settings(load_settings())          # keeps existing settings, fills in new ones
    script = os.path.join(APP_DIR, "netusage.py")

    print("2/6  Reading Windows' usage log (can take a minute)...")
    class A: srum = None
    code = cmd_collect(A())
    if code != 0:
        print("     The first read failed - see", LOG_PATH)

    print("3/6  Creating the hourly background task '%s'" % TASK_NAME)
    create_task(TASK_NAME, task_xml(pythonw, script))

    print("4/6  Creating the notifications task '%s'" % NOTIFY_TASK_NAME)
    create_task(NOTIFY_TASK_NAME, notify_task_xml(pythonw, script))

    print("5/6  Adding 'Data Usage' and 'NetUsage Settings' to your desktop and Start menu")
    os.makedirs(START_MENU_DIR, exist_ok=True)
    gear = os.path.join(_SYS32, "shell32.dll") + ",316"
    for folder in (desktop_dir(), START_MENU_DIR):
        write_url_shortcut(os.path.join(folder, SHORTCUT_NAME))
        make_lnk(os.path.join(folder, SETTINGS_SHORTCUT), pythonw, '"%s" settings' % script,
                 "Data limit, alerts and update checks for NetUsage", gear)

    print("6/6  Done.\n")
    cmd_status(None)
    print("\nOpening the dashboard...")
    open_dashboard()
    return 0


def cmd_uninstall(args):
    for name in (TASK_NAME, NOTIFY_TASK_NAME):
        run(["schtasks", "/delete", "/tn", name, "/f"])
    for path in (os.path.join(desktop_dir(), SHORTCUT_NAME), os.path.join(desktop_dir(), SETTINGS_SHORTCUT)):
        try:
            os.remove(path)
        except OSError:
            pass
    shutil.rmtree(START_MENU_DIR, ignore_errors=True)
    if IS_WINDOWS:
        try:
            import winreg
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, "Software\\Classes\\AppUserModelId\\" + AUMID)
        except OSError:
            pass
    print("Background tasks and shortcuts removed.")
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
    sub.add_parser("settings")
    n = sub.add_parser("notify")
    n.add_argument("--test", action="store_true", help="show a test notification")
    args = p.parse_args()
    return {"collect": cmd_collect, "report": cmd_report, "status": cmd_status,
            "install": cmd_install, "uninstall": cmd_uninstall,
            "settings": cmd_settings, "notify": cmd_notify}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
