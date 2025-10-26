#!/usr/bin/env python3
# reaper_portal_fc.py
#
# Generic xdg-desktop-portal FileChooser (Gio/DBus only)
#
# Responsibilities:
#   - Accepts ONLY command-line arguments (no JSON input)
#   - Prints JSON to stdout on success: {"path", "paths", "choices"}
#   - X11 parenting: deterministic via process-ancestor chain; modal=False (no dimming/locking)
#   - current_folder is passed strictly per spec as 'ay' (NUL-terminated path)
#   - Symlinks are NOT resolved (no realpath/resolve) — we use expanded/abspath strings
#   - If the requested start folder is invalid/missing -> fall back to $HOME
#
# Arguments:
#   --title "Open project"
#   --accept-label "_Open"
#   --multiple
#   --directory                      (uses SelectFolder)
#   --save                           (uses SaveFile; otherwise OpenFile)
#   --modal
#   --current-folder "/path/to/dir"  (sent as 'ay'; falls back to $HOME if invalid/empty)
#   --current-file   "/path/to/file" (sent as 'ay'; only applied for --save; no existence check)
#   --current-name   "Name.rpp"      (string; only applied for --save)
#   --filter "Label|glob1;glob2;..." (repeatable)
#   --initial-filter "Label"
#   --choice "id|label|default"      (default in {true,false,1,0,yes,no}; repeatable)
#   --parent "x11:0x..." | "wayland:HANDLE"
#   --timeout 0                      (0 = NO timeout [default], >0 = seconds)
#   --out -                          ('-' = stdout)
#   --err -                          (optional debug log; omit in production)
#
# Notes:
#   - This script focuses on the *how* (portal call, parenting, marshalling).
#     The Lua scripts define the *what* (filters, choices, labels, etc.).

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
from urllib.parse import unquote

import gi
gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib


# =============================================================================
# I/O utilities
# =============================================================================

def write_json(obj, out_target: str) -> None:
    """Write JSON to stdout or to a file atomically."""
    data = json.dumps(obj)
    if out_target == "-":
        sys.stdout.write(data)
        sys.stdout.flush()
    else:
        tmp = f"{out_target}.tmp-{int(time.time() * 1e6)}"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, out_target)


def log_err(msg: str, err_target: str | None) -> None:
    """Append a line to the error target (stderr or file)."""
    if not err_target:
        return
    line = msg if msg.endswith("\n") else msg + "\n"
    if err_target == "-":
        try:
            sys.stderr.write(line)
            sys.stderr.flush()
        except Exception:
            pass
    else:
        try:
            with open(err_target, "a", encoding="utf-8") as f:
                f.write(line)
        except Exception:
            pass


def which(cmd: str) -> bool:
    """Return True if executable is found in PATH."""
    return shutil.which(cmd) is not None


# =============================================================================
# /proc helpers (build ancestor set; used to pinpoint the correct X11 parent)
# =============================================================================

def _read_text(path: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    except Exception:
        return None


def _ppid(pid: int) -> int | None:
    """Return parent PID for a given pid."""
    t = _read_text(f"/proc/{pid}/status")
    if not t:
        return None
    for line in t.splitlines():
        if line.startswith("PPid:"):
            try:
                return int(line.split()[1])
            except Exception:
                return None
    return None


def _comm(pid: int) -> str | None:
    t = _read_text(f"/proc/{pid}/comm")
    return t.strip() if t else None


def _cmdline(pid: int) -> str | None:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            parts = [p.decode("utf-8", "ignore") for p in f.read().split(b"\x00") if p]
        return " ".join(parts) if parts else None
    except Exception:
        return None


def _exe(pid: int) -> str | None:
    try:
        return os.readlink(f"/proc/{pid}/exe")
    except Exception:
        return None


def collect_ancestors() -> tuple[set[int], set[int]]:
    """
    Walk up the process tree from our parent and build:
      - anc: all ancestor PIDs
      - reaper_anc: subset where name/exe/cmdline hints at 'reaper'
    """
    anc, reaper_anc = set(), set()
    pid = os.getppid()
    seen = set()
    while pid and pid not in seen and pid > 0:
        seen.add(pid)
        anc.add(pid)
        name = (_comm(pid) or "").lower()
        exe = (_exe(pid) or "").lower()
        cmd = (_cmdline(pid) or "").lower()
        if ("reaper" in name) or ("reaper" in exe) or (" reaper" in cmd) or cmd.startswith("reaper"):
            reaper_anc.add(pid)
        pid = _ppid(pid)
    return anc, reaper_anc


# =============================================================================
# X11 helpers (xprop), window selection for parenting
# =============================================================================

def _xprop(args: list[str]) -> str:
    try:
        return subprocess.check_output(["xprop"] + args, text=True, stderr=subprocess.DEVNULL)
    except Exception:
        return ""


def _parse_ids(s: str) -> list[str]:
    """Extract hex window ids (e.g., '0x5c02b2e') from xprop output."""
    ids = []
    for token in s.replace("\n", " ").split(","):
        token = token.strip()
        if not token:
            continue
        wid = token.split()[-1]
        if wid.startswith("0x"):
            ids.append(wid.lower())
    return ids


def _pid_of_win(wid: str) -> int | None:
    s = _xprop(["-id", wid, "_NET_WM_PID"])
    if "=" in s:
        try:
            return int(s.split("=")[-1].strip())
        except Exception:
            return None
    return None


def _wm_class_has_reaper(wid: str) -> bool:
    s = _xprop(["-id", wid, "WM_CLASS"])
    m = re.search(r'WM_CLASS\(.*\)\s*=\s*(.+)$', s)
    if not m:
        return False
    return "reaper" in m.group(1).lower()


def _types(wid: str) -> list[str]:
    s = _xprop(["-id", wid, "_NET_WM_WINDOW_TYPE"])
    return [t.strip() for t in s.split("=")[-1].split(",")] if "=" in s else []


def _is_normal(wid: str) -> bool:
    return any("_NET_WM_WINDOW_TYPE_NORMAL" in t for t in _types(wid))


def _has_transient_for(wid: str) -> bool:
    s = _xprop(["-id", wid, "WM_TRANSIENT_FOR"])
    return "window id" in s.lower()


def detect_parent_x11_via_anc(err_target: str | None) -> str | None:
    """
    Select a stable X11 parent window belonging to our process tree (prefer REAPER ancestors).
    Returns 'x11:0xABC...' or None.
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

    ancestors, reaper_anc = collect_ancestors()

    # Prefer stacking order for "front-most" candidate; fall back to client list.
    stack = _xprop(["-root", "_NET_CLIENT_LIST_STACKING"])
    ids = list(reversed(_parse_ids(stack)))
    if not ids:
        cl = _xprop(["-root", "_NET_CLIENT_LIST"])
        ids = _parse_ids(cl)

    best = None
    best_pref = -1  # 2: pid in reaper_anc; 1: pid in ancestors

    for wid in ids:
        if not _is_normal(wid):
            continue
        if _has_transient_for(wid):
            continue
        if not _wm_class_has_reaper(wid):
            continue
        pid = _pid_of_win(wid)
        if pid is None:
            continue
        if pid not in ancestors:
            continue
        pref = 2 if pid in reaper_anc else 1
        if pref > best_pref:
            best_pref = pref
            best = wid
            if pref == 2:
                break

    return ("x11:" + best) if best else None


# =============================================================================
# Path -> 'ay' marshalling (NUL-terminated); symlinks NOT resolved
# =============================================================================

def ay_dir_or_home(given_path: str | None) -> GLib.Variant:
    """
    Return GLib.Variant('ay') for a directory path (NUL-terminated).
    - DO NOT resolve symlinks
    - If given_path is invalid/missing -> $HOME
    """
    home = os.path.expanduser("~")
    if given_path:
        p = os.path.abspath(os.path.expanduser(given_path))
        s = p if os.path.isdir(p) else home
    else:
        s = home
    b = os.fsencode(s) + b"\x00"
    return GLib.Variant('ay', b)


def ay_file_from_path(given_path: str | None) -> GLib.Variant | None:
    """
    Return GLib.Variant('ay') for a file path (NUL-terminated).
    - DO NOT resolve symlinks
    - No existence check (useful for planned save flows)
    """
    if not given_path:
        return None
    s = os.path.abspath(os.path.expanduser(given_path))
    b = os.fsencode(s) + b"\x00"
    return GLib.Variant('ay', b)


# =============================================================================
# Argument -> Variant conversion helpers
# =============================================================================

def parse_filter_arg(s: str):
    """
    Parse a filter of form: "Label|glob1;glob2;glob3"
      -> returns (label: str, entries: list[(0, glob)])
    """
    if "|" not in s:
        return None
    label, rest = s.split("|", 1)
    globs = [g.strip() for g in rest.split(";") if g.strip()]
    if not label.strip() or not globs:
        return None
    entries = [(0, g) for g in globs]  # 0 = glob
    return (label.strip(), entries)


def parse_choice_arg(s: str):
    """
    Parse a choice of form: "id|label|default"
      default in {true,false,1,0,yes,no} -> stored as "true"/"false" string
      returns tuple matching a(ssa(ss)s)
    """
    parts = s.split("|")
    if len(parts) < 2:
        return None
    cid = parts[0].strip()
    lab = parts[1].strip() or cid
    dft = (parts[2].strip().lower() if len(parts) >= 3 else "false")
    truthy = {"true", "1", "yes", "y", "on"}
    default = "true" if dft in truthy else "false"
    return (cid, lab, [], default)  # a(ssa(ss)s)


# =============================================================================
# Portal call (DBus)
# =============================================================================

def open_via_portal(args, parent: str | None) -> dict:
    """
    Invoke org.freedesktop.portal.FileChooser.{OpenFile|SaveFile|SelectFolder} via Gio/DBus.
    Returns a dict with keys: 'paths': [str], 'choices': {id: bool}, 'done': bool
    """
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)

    fc = Gio.DBusProxy.new_sync(
        bus, Gio.DBusProxyFlags.DO_NOT_LOAD_PROPERTIES, None,
        'org.freedesktop.portal.Desktop',
        '/org/freedesktop/portal/desktop',
        'org.freedesktop.portal.FileChooser',
        None
    )

    title = args.title or "Open"

    # Decide which method to call
    if args.directory:
        method = 'SelectFolder'
    elif args.save:
        method = 'SaveFile'
    else:
        method = 'OpenFile'

    # Base options
    opts: dict[str, GLib.Variant] = {
        'multiple': GLib.Variant('b', bool(args.multiple)),
        'modal': GLib.Variant('b', bool(args.modal)),
        # Always set current_folder per spec (ay), with a $HOME fallback.
        'current_folder': ay_dir_or_home(args.current_folder),
    }

    # Optional label
    if args.accept_label:
        opts['accept_label'] = GLib.Variant('s', args.accept_label)

    # Choices
    if args.choice:
        items = []
        for c in args.choice:
            tup = parse_choice_arg(c)
            if tup:
                items.append(tup)
        if items:
            opts['choices'] = GLib.Variant('a(ssa(ss)s)', items)

    # Filters (and optional current_filter)
    current_filter_tuple = None
    if args.filter:
        filters = []
        for f in args.filter:
            tup = parse_filter_arg(f)
            if tup:
                filters.append(tup)
        if filters:
            opts['filters'] = GLib.Variant('a(sa(us))', filters)
            if args.initial_filter:
                for label, entries in filters:
                    if label == args.initial_filter:
                        current_filter_tuple = (label, entries)
                        break
    if current_filter_tuple:
        opts['current_filter'] = GLib.Variant('(sa(us))', current_filter_tuple)

    # Save-only options
    if args.save:
        # Optional current_file (ay) — often honored if the file exists
        if args.current_file:
            v = ay_file_from_path(args.current_file)
            if v:
                opts['current_file'] = v
        # Optional current_name (string) — suggested file name
        if args.current_name:
            opts['current_name'] = GLib.Variant('s', args.current_name)

    # Call the method
    params = GLib.Variant('(ssa{sv})', (parent or '', title, opts))
    res = fc.call_sync(method, params, 0, -1, None)
    req_path = res.unpack()[0]

    # Listen for the async response
    req = Gio.DBusProxy.new_sync(
        bus, Gio.DBusProxyFlags.DO_NOT_LOAD_PROPERTIES, None,
        'org.freedesktop.portal.Desktop', req_path, 'org.freedesktop.portal.Request', None
    )

    result = {'paths': [], 'choices': {}, 'done': False}
    loop = GLib.MainLoop()

    def on_resp(_proxy, _sender, signal, params):
        if signal != 'Response':
            return
        try:
            code, a = params.unpack()  # (u, a{sv})
            if code == 0:
                uris = a.get('uris', [])
                ch = a.get('choices', {})
                # URIs -> file paths
                for uri in uris or []:
                    if isinstance(uri, str) and uri.startswith("file://"):
                        result['paths'].append(unquote(uri[7:]))
                    elif isinstance(uri, str):
                        result['paths'].append(uri)
                # Choices -> {id: bool}
                if isinstance(ch, dict):
                    result['choices'] = {k: (v == 'true') for k, v in ch.items()}
                elif isinstance(ch, (list, tuple)):
                    tmp = {}
                    for item in ch:
                        if isinstance(item, (list, tuple)) and len(item) >= 2:
                            k, v = item[0], item[1]
                            if isinstance(k, str) and isinstance(v, str):
                                tmp[k] = (v == 'true')
                    result['choices'] = tmp
            result['done'] = True
        finally:
            loop.quit()

    req.connect('g-signal', on_resp)

    # Optional timeout: only if explicitly > 0 (default is 0 = no timeout)
    if args.timeout and args.timeout > 0:
        def on_timeout():
            if not result['done']:
                result['done'] = True
                loop.quit()
            return False
        GLib.timeout_add_seconds(args.timeout, on_timeout)

    loop.run()
    return result


# =============================================================================
# Main
# =============================================================================

def main() -> int:
    ap = argparse.ArgumentParser(description="Generic portal-backed file chooser (DBus/Gio)")
    ap.add_argument("--out", required=True, help="'-' for stdout")
    ap.add_argument("--err", default=None, help="'-' for stderr (debug only; omit otherwise)")

    # Dialog
    ap.add_argument("--title", default="Open")
    ap.add_argument("--accept-label")
    ap.add_argument("--multiple", action="store_true")
    ap.add_argument("--directory", action="store_true")
    ap.add_argument("--save", action="store_true", help="Use SaveFile instead of OpenFile")
    ap.add_argument("--modal", action="store_true")

    # Start folder/file (spec-compliant ay)
    ap.add_argument("--current-folder", help="Start directory; passed as ay (NUL-terminated). Fallback: $HOME.")
    ap.add_argument("--current-file", help="Start file (SaveFile only); passed as ay (NUL-terminated).")
    ap.add_argument("--current-name", help="Suggested file name (SaveFile only; plain string).")

    # Filters & choices
    ap.add_argument("--filter", action="append", help='Format: "Label|glob1;glob2;..." (repeatable)')
    ap.add_argument("--initial-filter", help="Label of one of the provided --filter entries")
    ap.add_argument("--choice", action="append", help='Format: "id|label|default" (default=true/false; repeatable)')

    # Plumbing
    ap.add_argument("--parent", default=None, help="x11:0x… or wayland:HANDLE (override)")
    ap.add_argument("--timeout", type=int, default=0, help="0 = no timeout (default); >0 = seconds")

    args = ap.parse_args()

    try:
        # Auto-detect X11 parent (deterministic via ancestor chain)
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
