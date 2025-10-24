#!/usr/bin/env python3
import os, sys, json, time, argparse, traceback

def write_json_atomic(path, obj):
    data = json.dumps(obj)
    tmp = f"{path}.tmp-{int(time.time()*1e6)}"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="Pfad zur JSON-Ausgabedatei")
    ap.add_argument("--err", required=False, help="Pfad für Fehler-Log (Text)")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    def log_err(msg):
        if not args.err:
            return
        try:
            with open(args.err, "a", encoding="utf-8") as f:
                f.write(msg + "\n")
        except Exception:
            pass

    if args.selftest:
        write_json_atomic(args.out, {"selftest": True, "ok": True})
        return 0

    os.environ.setdefault("GTK_USE_PORTAL", "1")

    try:
        import gi
    except Exception as e:
        write_json_atomic(args.out, {"error": f"python3-gi not available: {e}"})
        return 1

    # GTK4 bevorzugen, sonst GTK3
    GTK4 = False
    try:
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk, GLib, Gdk
        GTK4 = True
    except Exception:
        try:
            gi.require_version("Gtk", "3.0")
            from gi.repository import Gtk, GLib, Gdk
            GTK4 = False
        except Exception as e:
            write_json_atomic(args.out, {"error": f"GTK not available: {e}"})
            return 1

    try:
        if Gdk.Display.get_default() is None:
            write_json_atomic(args.out, {"error": "No display (Gdk.Display.get_default() is None)"})
            return 1
    except Exception as e:
        write_json_atomic(args.out, {"error": f"Display init failed: {e}"})
        return 1

    try:
        title = "Open project (Portal)"
        dlg = Gtk.FileChooserNative.new(title, None, Gtk.FileChooserAction.OPEN, "_Open", "_Cancel")

        # Filter
        all_supported = ["*.RPP","*.TXT","*.EDL","PROJ*.TXT","*.ADL","clipsort.log","*.RPP-BAK"]
        ff_all = Gtk.FileFilter(); ff_all.set_name("All Supported Projects")
        for pat in all_supported: ff_all.add_pattern(pat)
        (dlg.set_filter if GTK4 else dlg.add_filter)(ff_all)

        filters = [
            ("REAPER Project files (*.RPP)", ["*.RPP"]),
            ("EDL TXT (Vegas) files (*.TXT)", ["*.TXT"]),
            ("EDL (Samplitude) files (*.EDL)", ["*.EDL"]),
            ("RADAR Session TXT files (PROJ*.TXT)", ["PROJ*.TXT"]),
            ("AES-31 files (*.ADL)", ["*.ADL"]),
            ("NINJAM log files (clipsort.log)", ["clipsort.log"]),
            ("REAPER Project Backup files (*.RPP-BAK)", ["*.RPP-BAK"]),
            ("All files (*.*)", ["*.*"]),
        ]
        for name, pats in filters:
            f = Gtk.FileFilter(); f.set_name(name)
            for p in pats: f.add_pattern(p)
            dlg.add_filter(f)

        # Choices (Checkboxen)
        try:
            if GTK4:
                Gtk.FileChooser.add_choice(dlg, "open_in_new_tab", "Open in new project tab", None, None)
                Gtk.FileChooser.set_choice(dlg, "open_in_new_tab", "false")
                Gtk.FileChooser.add_choice(dlg, "fx_offline", "Open with FX offline (recovery mode)", None, None)
                Gtk.FileChooser.set_choice(dlg, "fx_offline", "false")
            else:
                dlg.add_choice("open_in_new_tab", "Open in new project tab", None, None)
                dlg.set_choice("open_in_new_tab", "false")
                dlg.add_choice("fx_offline", "Open with FX offline (recovery mode)", None, None)
                dlg.set_choice("fx_offline", "false")
        except Exception as e:
            log_err(f"choices not supported by backend: {e}")

        loop = GLib.MainLoop()
        result = {"path": None, "choices": {"open_in_new_tab": False, "fx_offline": False}}

        def get_choice(key):
            try:
                if GTK4:
                    return (Gtk.FileChooser.get_choice(dlg, key) == "true")
                else:
                    return (dlg.get_choice(key) == "true")
            except Exception:
                return False

        def on_resp(native, response):
            try:
                if response == Gtk.ResponseType.ACCEPT:
                    if GTK4:
                        f = native.get_file()
                        path = f.get_path() if f else None
                    else:
                        path = native.get_filename()
                    result["path"] = path
                    result["choices"]["open_in_new_tab"] = get_choice("open_in_new_tab")
                    result["choices"]["fx_offline"] = get_choice("fx_offline")
            finally:
                try:
                    native.destroy()
                finally:
                    loop.quit()

        dlg.connect("response", on_resp)
        dlg.show()
        loop.run()

        write_json_atomic(args.out, result)
        return 0

    except Exception as e:
        tb = traceback.format_exc()
        if args.err:
            try:
                with open(args.err, "a", encoding="utf-8") as f:
                    f.write(tb + "\n")
            except Exception:
                pass
        write_json_atomic(args.out, {"error": repr(e)})
        return 1

if __name__ == "__main__":
    sys.exit(main())
