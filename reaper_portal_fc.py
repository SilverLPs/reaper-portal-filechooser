#!/usr/bin/env python3
# reaper_portal_fc.py
#
# Generic xdg-desktop-portal FileChooser (Gio/DBus only)
#
# Outputs JSON:
# {
#   "path", "paths",
#   "choices",       # booleans for checkbox entries (compat for easy use)
#   "choices_raw",   # strings for all entries (checkbox: "true"/"false"; select: selected option id)
#   "selected_filter_label",
#   "selected_filter_globs"
# }
#
# Highlights:
# - Use --option for ordered custom UI:
#     Checkbox: --option "check|id|Label|true"
#     Select:   --option "select|id|Label|optId1:Opt 1;optId2:Opt 2|defaultId"
# - Case-insensitive GTK filter workaround (duplicates *.EXT and *.ext internally),
#   while returning the original globs back in JSON (no duplicates).
#
# Notes:
# - For Open (incl. --directory), current_folder defaults to $HOME (spec 'ay').
# - For Save, current_folder is set IFF:
#     * --current-folder is provided, OR
#     * --current-file is NOT provided (fallback to $HOME).

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
# I/O
# =============================================================================

def write_json(obj, out_target: str) -> None:
    data = json.dumps(obj)
    if out_target == "-":
        sys.stdout.write(data)
        sys.stdout.flush()
    else:
        tmp = f"{out_target}.tmp-{int(time.time()*1e6)}"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, out_target)


def log_err(msg: str, err_target: str | None) -> None:
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
    return shutil.which(cmd) is not None


# =============================================================================
# /proc helpers (X11 parenting)
# =============================================================================

def _read_text(path: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    except Exception:
        return None


def _ppid(pid: int) -> int | None:
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
    anc, reaper_anc = set(), set()
    pid = os.getppid()
    seen = set()
    while pid and pid not in seen and pid > 0:
        seen.add(pid)
        anc.add(pid)
        name = (_comm(pid) or "").lower()
        exe  = (_exe(pid)  or "").lower()
        cmd  = (_cmdline(pid) or "").lower()
        if ("reaper" in name) or ("reaper" in exe) or (" reaper" in cmd) or cmd.startswith("reaper"):
            reaper_anc.add(pid)
        pid = _ppid(pid)
    return anc, reaper_anc


# =============================================================================
# X11 helpers
# =============================================================================

def _xprop(args: list[str]) -> str:
    try:
        return subprocess.check_output(["xprop"] + args, text=True, stderr=subprocess.DEVNULL)
    except Exception:
        return ""


def _parse_ids(s: str) -> list[str]:
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
# Path -> 'ay'
# =============================================================================

def ay_dir_or_home(given_path: str | None) -> GLib.Variant:
    home = os.path.expanduser("~")
    if given_path:
        p = os.path.abspath(os.path.expanduser(given_path))
        s = p if os.path.isdir(p) else home
    else:
        s = home
    b = os.fsencode(s) + b"\x00"
    return GLib.Variant('ay', b)


def ay_file_from_path(given_path: str | None) -> GLib.Variant | None:
    if not given_path:
        return None
    s = os.path.abspath(os.path.expanduser(given_path))
    b = os.fsencode(s) + b"\x00"
    return GLib.Variant('ay', b)


# =============================================================================
# Options (checkbox/select)
# =============================================================================

def parse_option_arg(s: str):
    """
    Checkbox: 'check|id|Label|true'  or 'checkbox|id|Label|false'
    Select:   'select|id|Label|optId1:Opt 1;optId2:Opt 2|defaultId'
    Returns (id, label, options:list[(id,label)], defaultStr)
      - For checkbox, options=[]
      - For select,  options=[(optId,optLabel),...], defaultStr=selected optId
    """
    parts = s.split("|", 3)  # allow labels with '|'
    if len(parts) < 3:
        return None
    typ = parts[0].strip().lower()
    cid = parts[1].strip()
    lab = parts[2].strip() or cid

    if typ in ("check", "checkbox"):
        dft = (parts[3].strip().lower() if len(parts) >= 4 else "false")
        truthy = {"true", "1", "yes", "y", "on"}
        default = "true" if dft in truthy else "false"
        return (cid, lab, [], default)

    if typ in ("select", "dropdown", "combo"):
        if len(parts) < 4:
            return None
        opt_and_default = parts[3]
        if "|" in opt_and_default:
            opt_str, default_id = opt_and_default.rsplit("|", 1)
        else:
            opt_str, default_id = opt_and_default, ""
        options = []
        for tok in opt_str.split(";"):
            tok = tok.strip()
            if not tok:
                continue
            if ":" in tok:
                oid, olab = tok.split(":", 1)
                options.append((oid.strip(), olab.strip()))
            else:
                oid = tok.strip()
                options.append((oid, oid))
        default = default_id.strip() if default_id.strip() else (options[0][0] if options else "")
        return (cid, lab, options, default)

    return None


# =============================================================================
# Filters (GTK case-insensitive dup)
# =============================================================================

def _dupe_case_globs(entries: list[tuple[int, str]]) -> list[tuple[int, str]]:
    out = []
    seen = set()
    for kind, pat in entries:
        if kind != 0 or not isinstance(pat, str):
            continue
        ups = pat.upper()
        lows = pat.lower()
        for variant in (ups, lows):
            key = (kind, variant)
            if key not in seen:
                seen.add(key)
                out.append(key)
    return out


# =============================================================================
# Portal call
# =============================================================================

def open_via_portal(args, parent: str | None) -> dict:
    """
    Invoke org.freedesktop.portal.FileChooser.{OpenFile|SaveFile}.
    --directory is implemented via OpenFile + options['directory']=true.
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

    if args.save:
        method = 'SaveFile'
    else:
        method = 'OpenFile'

    opts: dict[str, GLib.Variant] = {
        'multiple': GLib.Variant('b', bool(args.multiple)),
        'modal':    GLib.Variant('b', bool(args.modal)),
    }

    # Directory selection (folders instead of files) for OpenFile
    if method == 'OpenFile' and args.directory:
        opts['directory'] = GLib.Variant('b', True)

    # current_folder
    if method == 'OpenFile':
        # Includes normal open AND directory mode
        opts['current_folder'] = ay_dir_or_home(args.current_folder)
    else:
        # SaveFile
        if args.current_folder:
            opts['current_folder'] = ay_dir_or_home(args.current_folder)
        elif not args.current_file:
            opts['current_folder'] = ay_dir_or_home(None)

    if args.accept_label:
        opts['accept_label'] = GLib.Variant('s', args.accept_label)

    # ----- Build ordered choices from --option (preserve CLI order) -----
    choice_items = []
    argv = sys.argv[1:]
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--option" and i + 1 < len(argv):
            parsed = parse_option_arg(argv[i + 1])
            if parsed:
                choice_items.append(parsed)
            i += 2
            continue
        # generic skip of flag+value
        if tok.startswith("--") and (i + 1) < len(argv) and not argv[i + 1].startswith("--"):
            i += 2
        else:
            i += 1

    if choice_items:
        # a(ssa(ss)s)
        opts['choices'] = GLib.Variant(
            'a(ssa(ss)s)',
            [(cid, lab, options, default) for (cid, lab, options, default) in choice_items]
        )

    # ----- Filters (expand for GTK, keep originals for returning) -----
    original_globs_by_label = {}
    current_filter_tuple = None
    if args.filter:
        filters_expanded = []
        for f in args.filter:
            if "|" not in f:
                continue
            label, rest = f.split("|", 1)
            globs = [g.strip() for g in rest.split(";") if g.strip()]
            if not label.strip() or not globs:
                continue
            entries = [(0, g) for g in globs]
            original_globs_by_label[label] = [pat for _, pat in entries]
            entries_expanded = _dupe_case_globs(entries)
            filters_expanded.append((label, entries_expanded))
        if filters_expanded:
            opts['filters'] = GLib.Variant('a(sa(us))', filters_expanded)
            if args.initial_filter:
                for label, entries_expanded in filters_expanded:
                    if label == args.initial_filter:
                        current_filter_tuple = (label, entries_expanded)
                        break
    if current_filter_tuple:
        opts['current_filter'] = GLib.Variant('(sa(us))', current_filter_tuple)

    # Save-only extras
    if method == 'SaveFile':
        if args.current_file:
            v = ay_file_from_path(args.current_file)
            if v:
                opts['current_file'] = v
            if not args.current_name:
                try:
                    base = os.path.basename(os.path.abspath(os.path.expanduser(args.current_file)))
                except Exception:
                    base = None
                if base:
                    opts['current_name'] = GLib.Variant('s', base)
        if args.current_name:
            opts['current_name'] = GLib.Variant('s', args.current_name)

    # ---- Call ----
    params = GLib.Variant('(ssa{sv})', (parent or '', title, opts))
    res = fc.call_sync(method, params, 0, -1, None)
    req_path = res.unpack()[0]

    req = Gio.DBusProxy.new_sync(
        bus,
        Gio.DBusProxyFlags.DO_NOT_LOAD_PROPERTIES,
        None,
        'org.freedesktop.portal.Desktop',
        req_path,
        'org.freedesktop.portal.Request',
        None
    )

    result = {
        'paths': [],
        'choices': {},       # checkbox booleans
        'choices_raw': {},   # strings for all
        'selected_filter_label': None,
        'selected_filter_globs': None,
        'done': False
    }
    loop = GLib.MainLoop()

    def _unpack_current_filter(v):
        try:
            label, entries = (v.unpack() if isinstance(v, GLib.Variant) else v)
        except Exception:
            return None, None
        globs = []
        try:
            for e in entries or []:
                if isinstance(e, (list, tuple)) and len(e) >= 2:
                    kind, val = e[0], e[1]
                    if kind == 0 and isinstance(val, str):
                        globs.append(val)
        except Exception:
            pass
        return label, (globs if globs else None)

    def _map_backend_globs_to_originals(label: str | None, backend_globs: list[str] | None) -> list[str] | None:
        """
        Map backend-returned globs (which may include our GTK case-duplication)
        back to the *original* globs that were provided via --filter.
        If originals for the label are known, return them exactly (order + casing).
        Otherwise, return backend globs de-duplicated case-insensitively.
        """
        if not label:
            return backend_globs

        originals = original_globs_by_label.get(label)
        if originals:
            return list(originals)

        if not backend_globs:
            return backend_globs
        seen = set()
        out = []
        for g in backend_globs:
            key = g.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(g)
        return out

    def on_resp(_proxy, _sender, signal, params):
        if signal != 'Response':
            return
        try:
            code, a = params.unpack()
            if code == 0:
                for uri in a.get('uris', []) or []:
                    if isinstance(uri, str) and uri.startswith("file://"):
                        result['paths'].append(unquote(uri[7:]))
                    elif isinstance(uri, str):
                        result['paths'].append(uri)

                ch = a.get('choices', {})
                if isinstance(ch, dict):
                    for k, v in ch.items():
                        if not isinstance(k, str):
                            continue
                        if isinstance(v, str):
                            result['choices_raw'][k] = v
                            if v in ("true", "false"):
                                result['choices'][k] = (v == "true")
                elif isinstance(ch, (list, tuple)):
                    for item in ch:
                        if isinstance(item, (list, tuple)) and len(item) >= 2:
                            k, v = item[0], item[1]
                            if isinstance(k, str) and isinstance(v, str):
                                result['choices_raw'][k] = v
                                if v in ("true", "false"):
                                    result['choices'][k] = (v == "true")

                cf = a.get('current_filter')
                if cf:
                    label, globs = _unpack_current_filter(cf)
                    result['selected_filter_label'] = label
                    result['selected_filter_globs'] = _map_backend_globs_to_originals(label, globs)

            result['done'] = True
        finally:
            loop.quit()

    req.connect('g-signal', on_resp)

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
    ap.add_argument("--directory", action="store_true",
                    help="Select folders instead of files (Open mode only, via OpenFile+directory=true)")
    ap.add_argument("--save", action="store_true", help="Use SaveFile instead of OpenFile")
    ap.add_argument("--modal", action="store_true")

    # Start folder/file
    ap.add_argument("--current-folder", help="Start directory; passed as ay (NUL-terminated).")
    ap.add_argument("--current-file", help="Start file (SaveFile only); passed as ay (NUL-terminated).")
    ap.add_argument("--current-name", help="Suggested file name (SaveFile only; plain string).")

    # Filters & ordered options
    ap.add_argument("--filter", action="append", help='Format: "Label|glob1;glob2;..." (repeatable)')
    ap.add_argument("--initial-filter", help="Label of one of the provided --filter entries")
    ap.add_argument(
        "--option",
        action="append",
        help='Checkbox: "check|id|Label|true";  Select: "select|id|Label|optId1:Opt 1;optId2:Opt 2|defaultId"'
    )

    # Plumbing
    ap.add_argument("--parent", default=None, help="x11:0x… or wayland:HANDLE (override)")
    ap.add_argument("--timeout", type=int, default=0, help="0 = no timeout (default); >0 = seconds")

    args = ap.parse_args()

    try:
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
            "choices": result.get('choices') or {},
            "choices_raw": result.get('choices_raw') or {},
            "selected_filter_label": result.get('selected_filter_label'),
            "selected_filter_globs": result.get('selected_filter_globs'),
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
