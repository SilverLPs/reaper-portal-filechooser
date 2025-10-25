#!/usr/bin/env python3
# reaper_portal_fc.py
#
# Generischer xdg-desktop-portal FileChooser (Gio/DBus only)
# - Eingabe: NUR Kommandozeilenargumente (kein JSON-Input)
# - Ausgabe: JSON auf stdout ({"path","paths","choices"})
# - X11-Parenting deterministisch via Prozess-Ahnenkette; modal=False (kein Abdunkeln)
# - current_folder ausschließlich spec-konform als 'ay' (NUL-terminierter Pfad)
# - Symlinks werden NICHT aufgelöst (keine realpath/resolve), wir verwenden den (expanded/abspath) String
# - Fallback: wenn Zielordner ungültig/fehlt -> $HOME als Startordner
#
# Argumente:
#   --title "Open project"
#   --accept-label "_Open"
#   --multiple
#   --directory                (SelectFolder statt OpenFile)
#   --modal
#   --current-folder "/pfad/zum/ordner"    (als 'ay'; Fallback auf $HOME, wenn ungültig/leer)
#   --current-file   "/pfad/zu/datei"      (als 'ay'; keine Existenzprüfung, für spätere Save-Flows nützlich)
#   --filter "Label|glob1;glob2;..."       (mehrfach)
#   --initial-filter "Label"
#   --choice "id|label|default"            (default=true/false; mehrfach)
#   --parent "x11:0x..."
#   --timeout 0                            (0 = KEIN Timeout; >0 = Sekunden)
#   --out -                                ('-' = stdout)
#   --err -                                (nur Debug, sonst weglassen)

import os, sys, json, time, argparse, shutil, subprocess, traceback, re
from pathlib import Path
from urllib.parse import unquote
import gi
gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib

# -------- I/O --------
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
            with open(err_target, "a", encoding="utf-8") as f: f.write(line)
        except Exception: pass

def which(cmd): return shutil.which(cmd) is not None

# -------- /proc helpers (Ahnenkette) --------
def _read_text(path):
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    except Exception:
        return None

def _ppid(pid):
    t = _read_text(f"/proc/{pid}/status")
    if not t: return None
    for line in t.splitlines():
        if line.startswith("PPid:"):
            try: return int(line.split()[1])
            except Exception: return None
    return None

def _comm(pid):
    t = _read_text(f"/proc/{pid}/comm")
    return t.strip() if t else None

def _cmdline(pid):
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            parts = [p.decode("utf-8","ignore") for p in f.read().split(b"\x00") if p]
        return " ".join(parts) if parts else None
    except Exception:
        return None

def _exe(pid):
    try:
        return os.readlink(f"/proc/{pid}/exe")
    except Exception:
        return None

def collect_ancestors():
    anc, reaper_anc = set(), set()
    pid = os.getppid()
    seen = set()
    while pid and pid not in seen and pid > 0:
        seen.add(pid); anc.add(pid)
        name = (_comm(pid) or "").lower()
        exe  = (_exe(pid)  or "").lower()
        cmd  = (_cmdline(pid) or "").lower()
        if ("reaper" in name) or ("reaper" in exe) or (" reaper" in cmd) or cmd.startswith("reaper"):
            reaper_anc.add(pid)
        pid = _ppid(pid)
    return anc, reaper_anc

# -------- xprop helpers (X11) --------
def _xprop(args):
    try:
        return subprocess.check_output(["xprop"] + args, text=True, stderr=subprocess.DEVNULL)
    except Exception:
        return ""

def _parse_ids(s):
    ids=[]
    for token in s.replace("\n"," ").split(","):
        token=token.strip()
        if not token: continue
        wid = token.split()[-1]
        if wid.startswith("0x"): ids.append(wid.lower())
    return ids

def _pid_of_win(wid):
    s=_xprop(["-id", wid, "_NET_WM_PID"])
    if "=" in s:
        try: return int(s.split("=")[-1].strip())
        except Exception: return None
    return None

def _wm_class_has_reaper(wid):
    s=_xprop(["-id", wid, "WM_CLASS"])
    m=re.search(r'WM_CLASS\(.*\)\s*=\s*(.+)$', s)
    if not m: return False
    return "reaper" in m.group(1).lower()

def _types(wid):
    s=_xprop(["-id", wid, "_NET_WM_WINDOW_TYPE"])
    return [t.strip() for t in s.split("=")[-1].split(",")] if "=" in s else []

def _is_normal(wid):
    return any("_NET_WM_WINDOW_TYPE_NORMAL" in t for t in _types(wid))

def _has_transient_for(wid):
    s=_xprop(["-id", wid, "WM_TRANSIENT_FOR"])
    return "window id" in s.lower()

def detect_parent_x11_via_anc(err_target):
    if (os.getenv("XDG_SESSION_TYPE") or "").lower()=="wayland": return None
    if os.getenv("PORTAL_NO_PARENT")=="1": return None
    if not which("xprop"): return None

    forced=os.getenv("PORTAL_PARENT")
    if forced and forced.startswith("x11:"): return forced

    ancestors, reaper_anc = collect_ancestors()

    stack=_xprop(["-root","_NET_CLIENT_LIST_STACKING"])
    ids=list(reversed(_parse_ids(stack)))
    if not ids:
        cl=_xprop(["-root","_NET_CLIENT_LIST"])
        ids=_parse_ids(cl)

    best=None
    best_pref=-1  # 2: pid in reaper_anc; 1: pid in ancestors

    for wid in ids:
        if not _is_normal(wid): continue
        if _has_transient_for(wid): continue
        if not _wm_class_has_reaper(wid): continue
        pid=_pid_of_win(wid)
        if pid is None: continue
        if pid not in ancestors: continue
        pref=2 if pid in reaper_anc else 1
        if pref>best_pref:
            best_pref=pref; best=wid
            if pref==2: break

    return ("x11:"+best) if best else None

# -------- Pfad -> 'ay' (NUL-terminierter Pfad); Symlinks NICHT auflösen --------
def ay_dir_or_home(given_path):
    """
    Liefert GLib.Variant('ay', nul-terminierter Pfad).
    - Verwendet KEIN realpath/resolve (Symlinks bleiben als String erhalten).
    - Existenzprüfung: wenn 'given_path' kein existierendes Verzeichnis -> $HOME.
    - Wenn 'given_path' leer/None -> $HOME.
    """
    home = os.path.expanduser("~")
    if given_path:
        p = os.path.abspath(os.path.expanduser(given_path))
        s = p if os.path.isdir(p) else home
    else:
        s = home
    b = os.fsencode(s) + b"\x00"
    return GLib.Variant('ay', b)

def ay_file_from_path(given_path):
    """
    GLib.Variant('ay') für eine Datei (Pfad als eingegebener String, expanduser+abspath,
    KEINE Existenzprüfung, KEIN resolve()) – nützlich für Save-Workflows.
    """
    if not given_path:
        return None
    s = os.path.abspath(os.path.expanduser(given_path))
    b = os.fsencode(s) + b"\x00"
    return GLib.Variant('ay', b)

# -------- Argument -> Variants --------
def parse_filter_arg(s):
    # "Label|glob1;glob2;glob3"
    if "|" not in s: return None
    label, rest = s.split("|", 1)
    globs = [g.strip() for g in rest.split(";") if g.strip()]
    if not label.strip() or not globs: return None
    entries = [(0, g) for g in globs]  # 0 = glob
    return (label.strip(), entries)

def parse_choice_arg(s):
    # "id|label|default"  default in {true,false,1,0,yes,no}
    parts = s.split("|")
    if len(parts) < 2: return None
    cid = parts[0].strip()
    lab = parts[1].strip() or cid
    dft = (parts[2].strip().lower() if len(parts)>=3 else "false")
    truthy = {"true","1","yes","y","on"}
    default = "true" if dft in truthy else "false"
    return (cid, lab, [], default)  # a(ssa(ss)s)

# -------- Portal --------
def open_via_portal(args, parent):
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    fc  = Gio.DBusProxy.new_sync(
        bus, Gio.DBusProxyFlags.DO_NOT_LOAD_PROPERTIES, None,
        'org.freedesktop.portal.Desktop',
        '/org/freedesktop/portal/desktop',
        'org.freedesktop.portal.FileChooser',
        None
    )

    title   = args.title or "Open"
    method  = 'SelectFolder' if args.directory else 'OpenFile'

    opts = {
        'multiple': GLib.Variant('b', bool(args.multiple)),
        'modal':    GLib.Variant('b', bool(args.modal)),
        # current_folder setzen wir IMMER (spec-konform als 'ay') – mit Home-Fallback
        'current_folder': ay_dir_or_home(args.current_folder),
    }

    if args.accept_label:
        opts['accept_label'] = GLib.Variant('s', args.accept_label)

    # choices
    if args.choice:
        items=[]
        for c in args.choice:
            tup = parse_choice_arg(c)
            if tup: items.append(tup)
        if items:
            opts['choices'] = GLib.Variant('a(ssa(ss)s)', items)

    # filters
    current_filter_tuple = None
    if args.filter:
        filters=[]
        for f in args.filter:
            tup = parse_filter_arg(f)
            if tup: filters.append(tup)
        if filters:
            opts['filters'] = GLib.Variant('a(sa(us))', filters)
            if args.initial_filter:
                for label, entries in filters:
                    if label == args.initial_filter:
                        current_filter_tuple = (label, entries)
                        break
    if current_filter_tuple:
        opts['current_filter'] = GLib.Variant('(sa(us))', current_filter_tuple)

    # optional: current_file als 'ay' (z. B. für spätere Save-Dialoge)
    if args.current_file:
        v = ay_file_from_path(args.current_file)
        if v:
            opts['current_file'] = v

    params = GLib.Variant('(ssa{sv})', (parent or '', title, opts))
    res = fc.call_sync(method, params, 0, -1, None)
    req_path = res.unpack()[0]

    req = Gio.DBusProxy.new_sync(
        bus, Gio.DBusProxyFlags.DO_NOT_LOAD_PROPERTIES, None,
        'org.freedesktop.portal.Desktop', req_path, 'org.freedesktop.portal.Request', None
    )

    result = {'paths': [], 'choices': {}, 'done': False}
    loop = GLib.MainLoop()

    def on_resp(proxy, sender, signal, params):
        if signal != 'Response': return
        try:
            code, a = params.unpack()  # (u, a{sv})
            if code == 0:
                uris = a.get('uris', [])
                ch   = a.get('choices', {})
                for uri in uris or []:
                    if isinstance(uri,str) and uri.startswith("file://"):
                        result['paths'].append(unquote(uri[7:]))
                    elif isinstance(uri,str):
                        result['paths'].append(uri)
                if isinstance(ch, dict):
                    result['choices'] = {k: (v == 'true') for k, v in ch.items()}
                elif isinstance(ch,(list,tuple)):
                    tmp={}
                    for item in ch:
                        if isinstance(item,(list,tuple)) and len(item)>=2:
                            k,v=item[0],item[1]
                            if isinstance(k,str) and isinstance(v,str):
                                tmp[k]=(v=='true')
                    result['choices']=tmp
            result['done'] = True
        finally:
            loop.quit()

    req.connect('g-signal', on_resp)

    # Timeout nur, wenn explizit > 0 übergeben wurde
    if args.timeout and args.timeout > 0:
        def on_timeout():
            if not result['done']:
                result['done'] = True
                loop.quit()
            return False
        GLib.timeout_add_seconds(args.timeout, on_timeout)

    loop.run()
    return result

# -------- main --------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out",     required=True, help="'-' für stdout")
    ap.add_argument("--err",     default=None, help="'-' für stderr' (Standard: keine Logs)")

    # Dialog-Definition:
    ap.add_argument("--title", default="Open")
    ap.add_argument("--accept-label")
    ap.add_argument("--multiple", action="store_true")
    ap.add_argument("--directory", action="store_true")
    ap.add_argument("--modal", action="store_true")

    # Startordner/Startdatei (spec-konform ay):
    ap.add_argument("--current-folder", help="Pfad zum Startordner; ay (NUL-terminiert). Fallback: $HOME.")
    ap.add_argument("--current-file",   help="Pfad zur Startdatei; ay (NUL-terminiert). Keine Existenzprüfung.")

    # Filter & Choices:
    ap.add_argument("--filter", action="append", help='Format: "Label|glob1;glob2;..." (mehrfach erlaubt)')
    ap.add_argument("--initial-filter", help="Label eines vorhandenen --filter")
    ap.add_argument("--choice", action="append", help='Format: "id|label|default" (default=true/false)')

    # Technik:
    ap.add_argument("--parent",  default=None, help="x11:0x… oder wayland:HANDLE (Override)")
    ap.add_argument("--timeout", type=int, default=0, help="0 = kein Timeout (Standard); >0 = Sekunden")

    args = ap.parse_args()

    try:
        # Parenting (X11), deterministisch via Ahnenkette
        parent = None
        if os.getenv("PORTAL_NO_PARENT") != "1":
            if args.parent:
                parent = args.parent
            else:
                if (os.getenv("XDG_SESSION_TYPE") or "").lower() != "wayland":
                    parent = detect_parent_x11_via_anc(args.err)

        result = open_via_portal(args, parent)

        paths = result.get('paths') or []
        single = paths[0] if paths else None
        out = {
            "path": single,
            "paths": paths,
            "choices": result.get('choices') or {}
        }
        write_json(out, args.out)
        return 0

    except Exception:
        if args.err:
            log_err("portal error:\n" + traceback.format_exc(), args.err)
        write_json({"error": "portal call failed"}, args.out)
        return 1

if __name__ == "__main__":
    sys.exit(main())
