#!/usr/bin/env python3
# reaper_portal_open.py
# Ein-Pfad-Helper: xdg-desktop-portal via Gio/DBus (keine GTK-Abhängigkeit)
# - JSON auf stdout (--out -)
# - robuste choices-Auswertung (Map ODER Liste)
# - korrekte Filter inkl. Start-Filter (a(sa(us)) / (sa(us)))
# - Auto-Parenting unter X11 (PPID -> XID via xprop), optional --parent
# - Flatpak-/Portal-kompatibel

import os, sys, json, time, argparse, shutil, subprocess, traceback
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

# ---------------- Parenting (X11) ----------------
def detect_x11_parent_from_ppid():
    # Best effort: per xprop die Fenster-XID des Elternprozesses (REAPER) finden
    if (os.getenv("XDG_SESSION_TYPE") or "").lower() == "wayland":
        return None
    if not which("xprop"):
        return None
    ppid = os.getppid()
    try:
        root = subprocess.check_output(
            ["xprop", "-root", "_NET_CLIENT_LIST"],
            text=True, stderr=subprocess.DEVNULL
        )
        ids = []
        for token in root.replace("\n", " ").split(","):
            token = token.strip()
            if not token: continue
            wid = token.split()[-1]
            if wid.startswith("0x"): ids.append(wid)
        for wid in ids:
            try:
                pid_out = subprocess.check_output(
                    ["xprop", "-id", wid, "_NET_WM_PID"],
                    text=True, stderr=subprocess.DEVNULL
                )
                pid = int(pid_out.split("=")[-1].strip())
                if pid == ppid:
                    return "x11:" + wid
            except Exception:
                continue
    except Exception:
        return None
    return None

# ---------------- Portal (Gio/DBus) ----------------
def make_choices_variant():
    # a(ssa(ss)s): (id, label, options[], default)
    return GLib.Variant('a(ssa(ss)s)', [
        ('open_in_new_tab', 'Open in new project tab', [], 'false'),
        ('fx_offline',      'Open with FX offline (recovery mode)', [], 'false'),
    ])

def make_filters_and_current():
    """
    filters : a(sa(us))
      - Element: (label: s, entries: a(us))
      - entry: (u, s) mit u = FileFilterType (0=glob, 1=mime)
    Rückgabe:
      filters_variant: GLib.Variant('a(sa(us))', ...)
      current_filter_tuple: ('All Supported Projects', a(us)-Einträge als Python-Liste)
    """
    GLOB = 0
    def E(pat):  # eine entry (glob, pattern)
        return (GLOB, pat)

    # WICHTIG: *kein* "*.*" in All Supported
    all_supported_globs = ["*.RPP","*.TXT","*.EDL","PROJ*.TXT","*.ADL","clipsort.log","*.RPP-BAK"]

    filters_py = [
        ('All Supported Projects',                  [E(p) for p in all_supported_globs]),
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
    current_filter_tuple = ('All Supported Projects', [E(p) for p in all_supported_globs])
    return filters_variant, current_filter_tuple

def normalize_choices(ch):
    """
    Portal kann 'choices' als a{ss} (Map) ODER a(ss) (Liste) liefern.
    Wir normalisieren auf dict[str,bool].
    """
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
    """
    org.freedesktop.portal.FileChooser.OpenFile(parent, title, options)
    Signatur: (s s a{sv}) -> (o)
    Rückgabe: (path:str|None, choices:dict)
    """
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    proxy = Gio.DBusProxy.new_sync(
        bus, Gio.DBusProxyFlags.DO_NOT_LOAD_PROPERTIES, None,
        'org.freedesktop.portal.Desktop',
        '/org/freedesktop/portal/desktop',
        'org.freedesktop.portal.FileChooser',
        None
    )

    filters_variant, current_filter_tuple = make_filters_and_current()

    # a{sv} als Python-Dict (Werte jeweils GLib.Variant)
    opts = {
        'multiple':       GLib.Variant('b', False),
        'choices':        make_choices_variant(),
        'filters':        filters_variant,                                # a(sa(us))
        'current_filter': GLib.Variant('(sa(us))', current_filter_tuple), # (sa(us))  <-- wichtig
        'modal':          GLib.Variant('b', True),
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
        if signal_name != 'Response':
            return
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
    ap.add_argument("--err",     default="-",  help="'-' für stderr'")
    ap.add_argument("--parent",  default=None, help="x11:0x… oder wayland:HANDLE (optional)")
    ap.add_argument("--timeout", type=int, default=120, help="Timeout in Sekunden (0=kein Timeout)")
    args = ap.parse_args()

    try:
        parent = args.parent or detect_x11_parent_from_ppid()
        title  = "Open project (Portal)"
        path, choices = open_file_via_portal(parent, title, args.timeout)

        if path:
            write_json({
                "path": path,
                "choices": {
                    "open_in_new_tab": bool(choices.get("open_in_new_tab")),
                    "fx_offline":      bool(choices.get("fx_offline")),
                }
            }, args.out)
            return 0
        else:
            write_json({"path": None, "choices": {}}, args.out)
            return 0

    except Exception:
        log_err("portal error:\n" + traceback.format_exc(), args.err)
        write_json({"error": "portal call failed"}, args.out)
        return 1

if __name__ == "__main__":
    sys.exit(main())
