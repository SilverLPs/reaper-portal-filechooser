#!/usr/bin/env python3
# reaper_portal_open.py
# Gio/DBus-only xdg-desktop-portal FileChooser für REAPER
# - JSON auf stdout (--out -)
# - strenges Parenting unter X11:
#   NORMAL, nicht transient, WM_CLASS enthält "reaper",
#   _NET_WM_PID gehört zur Ahnenkette des aktuellen Prozesses,
#   bevorzugt Ahnen, deren Name/Cmdline "reaper" enthält.
# - modal=False (kein Abdunkeln/Sperren)
# - korrekte Filter inkl. Start-Filter
# - standardmäßig keine Logs (stderr nur, wenn --err gesetzt)

import os, sys, json, time, argparse, shutil, subprocess, traceback, re

import gi
gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib

# ---------------- I/O ----------------
def write_json(obj, out_target):
    data = json.dumps(obj)
    if out_target == "-":
        sys.stdout.write(data); sys.stdout.flush()
    else:
        tmp = f"{out_target}.tmp-{int(time.time()*1e6)}"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(data); f.flush(); os.fsync(f.fileno())
        os.replace(tmp, out_target)

def log_err(msg, err_target):
    if not err_target: return
    line = msg if msg.endswith("\n") else msg + "\n"
    if err_target == "-":
        try: sys.stderr.write(line); sys.stderr.flush()
        except Exception: pass
    else:
        try:
            with open(err_target, "a", encoding="utf-8") as f:
                f.write(line)
        except Exception: pass

def which(cmd): return shutil.which(cmd) is not None

# ---------------- /proc helpers ----------------
def _read_file(path):
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    except Exception:
        return None

def _get_ppid(pid: int):
    txt = _read_file(f"/proc/{pid}/status")
    if not txt: return None
    for line in txt.splitlines():
        if line.startswith("PPid:"):
            try: return int(line.split()[1])
            except Exception: return None
    return None

def _get_comm(pid: int):
    txt = _read_file(f"/proc/{pid}/comm")
    if not txt: return None
    return txt.strip()

def _get_cmdline(pid: int):
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            data = f.read().split(b"\x00")
        parts = [p.decode("utf-8", "ignore") for p in data if p]
        return " ".join(parts) if parts else None
    except Exception:
        return None

def _get_exe(pid: int):
    try:
        return os.readlink(f"/proc/{pid}/exe")
    except Exception:
        return None

def _collect_ancestors():
    """
    Geh die Prozesskette hoch: aktueller PID -> PPID -> ... -> 1.
    Liefert:
      - ancestors: set[int] aller Ahnen-PIDs
      - reaper_anc: set[int] der Ahnen, deren Name/Exe/Cmdline 'reaper' enthält
    """
    ancestors = set()
    reaper_anc = set()
    pid = os.getppid()
    seen = set()
    while pid and pid not in seen and pid > 0:
        seen.add(pid)
        ancestors.add(pid)
        name = (_get_comm(pid) or "").lower()
        exe = (_get_exe(pid)  or "").lower()
        cmd = (_get_cmdline(pid) or "").lower()
        if ("reaper" in name) or ("reaper" in exe) or (" reaper" in cmd) or cmd.startswith("reaper"):
            reaper_anc.add(pid)
        pid = _get_ppid(pid)
    return ancestors, reaper_anc

# ---------------- xprop helpers ----------------
def _xprop(args):
    try:
        return subprocess.check_output(["xprop"] + args, text=True, stderr=subprocess.DEVNULL)
    except Exception:
        return ""

def _parse_hex_ids_from_xprop_list(s):
    ids = []
    for token in s.replace("\n"," ").split(","):
        token = token.strip()
        if not token: continue
        wid = token.split()[-1]
        if wid.startswith("0x"): ids.append(wid.lower())
    return ids

def _pid_of_window(wid):
    s = _xprop(["-id", wid, "_NET_WM_PID"])
    if "=" in s:
        try: return int(s.split("=")[-1].strip())
        except Exception: return None
    return None

def _wm_class_contains_reaper(wid):
    s = _xprop(["-id", wid, "WM_CLASS"])
    m = re.search(r'WM_CLASS\(.*\)\s*=\s*(.+)$', s)
    if not m: return False
    vals = m.group(1).lower()
    return "reaper" in vals

def _window_types(wid):
    s = _xprop(["-id", wid, "_NET_WM_WINDOW_TYPE"])
    return [t.strip() for t in s.split("=")[-1].split(",")] if "=" in s else []

def _is_normal(wid):
    return any("_NET_WM_WINDOW_TYPE_NORMAL" in t for t in _window_types(wid))

def _has_transient_for(wid):
    s = _xprop(["-id", wid, "WM_TRANSIENT_FOR"])
    return "window id" in s.lower()  # vorhanden ≈ transient

# ---------------- Parenting (X11 – streng & über Ahnenkette) ----------------
def detect_x11_parent_via_ancestors(err_target):
    """
    Liefert 'x11:0x…' nur, wenn ALLE Bedingungen erfüllt sind:
      - SESSION != wayland
      - xprop vorhanden
      - Fenster: TYPE_NORMAL, NICHT transient, WM_CLASS enthält 'reaper'
      - _NET_WM_PID gehört zur Ahnenkette des aktuellen Prozesses
      - bevorzugt: _NET_WM_PID ∈ reaper_anc (Ahnen, deren Name/Exe/Cmdline 'reaper' enthält)
    Sonst: None (kein Parenting).
    """
    if (os.getenv("XDG_SESSION_TYPE") or "").lower() == "wayland":
        return None
    if os.getenv("PORTAL_NO_PARENT") == "1":
        return None
    if not which("xprop"):
        return None

    forced = os.getenv("PORTAL_PARENT")
    if forced and forced.startswith("x11:"):
        return forced

    ancestors, reaper_anc = _collect_ancestors()

    # Kandidatenlisten: stacking (top-first) → client list
    stack = _xprop(["-root", "_NET_CLIENT_LIST_STACKING"])
    ids = list(reversed(_parse_hex_ids_from_xprop_list(stack)))
    if not ids:
        cl = _xprop(["-root", "_NET_CLIENT_LIST"])
        ids = _parse_hex_ids_from_xprop_list(cl)

    best = None
    best_pref = -1  # 2: pid in reaper_anc; 1: pid in ancestors; 0: sonst

    for wid in ids:
        if not _is_normal(wid):
            continue
        if _has_transient_for(wid):
            continue
        if not _wm_class_contains_reaper(wid):
            continue
        pid = _pid_of_window(wid)
        if pid is None:
            continue
        # harte Bedingung: pid muss in der Ahnenkette liegen
        if pid not in ancestors:
            continue
        # Präferenz: ist der Ahne „reaper“?
        pref = 2 if pid in reaper_anc else 1
        if pref > best_pref:
            best_pref = pref
            best = wid
            if pref == 2:
                break  # perfekter Match gefunden

    return ("x11:" + best) if best else None

# ---------------- Portal (Gio/DBus) ----------------
def make_choices_variant():
    return GLib.Variant('a(ssa(ss)s)', [
        ('open_in_new_tab', 'Open in new project tab', [], 'false'),
        ('fx_offline',      'Open with FX offline (recovery mode)', [], 'false'),
    ])

def make_filters_and_current():
    GLOB = 0
    def E(pat): return (GLOB, pat)
    all_supported = ["*.RPP","*.TXT","*.EDL","PROJ*.TXT","*.ADL","clipsort.log","*.RPP-BAK"]
    filters_py = [
        ('All Supported Projects',                  [E(p) for p in all_supported]),
        ('REAPER Project files (*.RPP)',            [E("*.RPP")]),
        ('EDL TXT (Vegas) files (*.TXT)',           [E("*.TXT")]),
        ('EDL (Samplitude) files (*.EDL)',          [E("*.EDL")]),
        ('RADAR Session TXT files (PROJ*.TXT)',     [E("PROJ*.TXT")]),
        ('AES-31 files (*.ADL)',                    [E("*.ADL")]),
        ('NINJAM log files (clipsort.log)',         [E("clipsort.log")]),
        ('REAPER Project Backup files (*.RPP-BAK)', [E("*.RPP-BAK")]),
        ('All files (*.*)',                         [E("*.*")]),
    ]
    filters_variant = GLib.Variant('a(sa(us))', filters_py)
    current_filter_tuple = ('All Supported Projects', [E(p) for p in all_supported])
    return filters_variant, current_filter_tuple

def normalize_choices(ch):
    if isinstance(ch, dict):
        return {k: (v == 'true') for k, v in ch.items()}
    if isinstance(ch, (list, tuple)):
        out = {}
        for item in ch:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                k, v = item[0], item[1]
                if isinstance(k, str) and isinstance(v, str):
                    out[k] = (v == 'true')
        return out
    return {}

def open_file_via_portal(parent, title, timeout_s):
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    proxy = Gio.DBusProxy.new_sync(
        bus, Gio.DBusProxyFlags.DO_NOT_LOAD_PROPERTIES, None,
        'org.freedesktop.portal.Desktop',
        '/org/freedesktop/portal/desktop',
        'org.freedesktop.portal.FileChooser',
        None
    )

    filters_variant, current_filter_tuple = make_filters_and_current()

    opts = {
        'multiple':       GLib.Variant('b', False),
        'choices':        make_choices_variant(),
        'filters':        filters_variant,
        'current_filter': GLib.Variant('(sa(us))', current_filter_tuple),
        'modal':          GLib.Variant('b', False),  # kein Abdunkeln/Sperren
    }

    params = GLib.Variant('(ssa{sv})', (parent or '', title, opts))
    res = proxy.call_sync('OpenFile', params, 0, -1, None)
    request_path = res.unpack()[0]

    req = Gio.DBusProxy.new_sync(
        bus, Gio.DBusProxyFlags.DO_NOT_LOAD_PROPERTIES, None,
        'org.freedesktop.portal.Desktop',
        request_path,
        'org.freedesktop.portal.Request',
        None
    )

    result = {'path': None, 'choices': {}, 'done': False}
    loop = GLib.MainLoop()

    def on_signal(proxy, sender, signal_name, params):
        if signal_name != 'Response': return
        try:
            code, a = params.unpack()  # (u, a{sv})
            if code == 0:
                uris = a.get('uris', [])
                ch   = a.get('choices', {})
                if uris:
                    uri = uris[0]
                    if uri.startswith("file://"):
                        from urllib.parse import unquote
                        result['path'] = unquote(uri[7:])
                    else:
                        result['path'] = uri
                result['choices'] = normalize_choices(ch)
            result['done'] = True
        finally:
            loop.quit()

    req.connect('g-signal', on_signal)

    if timeout_s and timeout_s > 0:
        def on_timeout():
            if not result['done']:
                result['done'] = True
                loop.quit()
            return False
        GLib.timeout_add_seconds(timeout_s, on_timeout)

    loop.run()
    return result['path'], result['choices']

# ---------------- main ----------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out",     required=True, help="'-' für stdout")
    ap.add_argument("--err",     default=None, help="'-' für stderr' (Standard: keine Logs)")
    ap.add_argument("--parent",  default=None, help="x11:0x… oder wayland:HANDLE (Override)")
    ap.add_argument("--no-parent", action="store_true", help="Parenting komplett deaktivieren")
    ap.add_argument("--timeout", type=int, default=120, help="Timeout in Sekunden (0=kein Timeout)")
    args = ap.parse_args()

    try:
        parent = None
        if not args.no_parent and os.getenv("PORTAL_NO_PARENT") != "1":
            if args.parent:
                parent = args.parent
            else:
                parent = detect_x11_parent_via_ancestors(args.err)

        title  = "Open project (Portal)"
        path, choices = open_file_via_portal(parent, title, args.timeout)

        if path:
            write_json({
                "path": path,
                "choices": {
                    "open_in_new_project_tab": bool(choices.get("open_in_new_tab")),
                    "fx_offline":              bool(choices.get("fx_offline")),
                }
            }, args.out)
            return 0
        else:
            write_json({"path": None, "choices": {}}, args.out)
            return 0

    except Exception:
        if args.err:
            log_err("portal error:\n" + traceback.format_exc(), args.err)
        write_json({"error": "portal call failed"}, args.out)
        return 1

if __name__ == "__main__":
    sys.exit(main())
