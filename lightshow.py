#!/usr/bin/env python3
"""LightShow for Omarchy - entry point.

    lightshow                 open the native desktop app
    lightshow daemon          run the engine in the background (no window)
    lightshow serve           run the web UI instead (localhost only)
    lightshow --port 9000     port for the web UI
    lightshow <effect>        apply an effect and exit (no server)
    lightshow list            list every effect
    lightshow fav <name>      load a saved favourite and exit
    lightshow day | night     load a profile and exit

The one-shot forms still go through the engine, so a hardware effect keeps
running after the process exits. A software effect needs the server, because
something has to be alive to step the animation.
"""

import argparse
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lightshow import effects, kbd, kb7  # noqa: E402
from lightshow.engine import Engine  # noqa: E402
from lightshow.server import serve  # noqa: E402


def open_browser(url):
    for cmd in (["omarchy-launch-browser", url], ["xdg-open", url], ["brave", url]):
        try:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except FileNotFoundError:
            continue
    return False


def cmd_list():
    print("HARDWARE (persist after exit, zero CPU)")
    for n in effects.HARDWARE:
        print(f"  {n:<12} {effects.HARDWARE_INFO.get(n,'')}")
    print("\nSOFTWARE (need the server running)")
    for n, (_, d) in effects.SOFTWARE.items():
        print(f"  {n:<12} {d}")


def main():
    ap = argparse.ArgumentParser(prog="lightshow", add_help=True,
                                 description="LightShow for Omarchy")
    ap.add_argument("action", nargs="?", default="gui")
    ap.add_argument("arg", nargs="?")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    if args.action == "list":
        return cmd_list()

    if args.action == "status":
        print("device : " + kbd.describe_device())
        others = kbd.scan_devices()
        if len(others) > 1:
            for n, ident, name in others:
                print(f"         also: {n}  {ident}  {name}")
        kb7_node = kb7.find_control_node()
        print("kb7    : " + (f"{kb7_node}  {kb7.VID}:{kb7.PID}  Turtle Beach Command Series KB7"
                              if kb7_node else "not connected"))
        print(f"theme  : {kbd.theme_name()}")
        print("palette: " + " ".join(kbd.rgb_hex(c) for c in kbd.theme_colors()))
        return

    # one-shot forms
    if args.action in ("day", "night"):
        e = Engine(); e.load_profile(args.action); time.sleep(0.3); return
    if args.action == "fav":
        e = Engine(); e.load_favorite(args.arg); time.sleep(0.3); return
    if args.action in effects.HARDWARE:
        e = Engine(); e.apply({"effect": args.action}); time.sleep(0.3); return
    if args.action in effects.SOFTWARE and args.action not in ("serve", "gui"):
        print(f"'{args.action}' is a software effect and needs the server.\n"
              f"Start it with: lightshow    (then pick {args.action} in the app)",
              file=sys.stderr)
        return 2

    if args.action == "daemon":
        # Headless: owns the device and keeps effects running with no UI.
        # Serves the same HTTP API so the desktop app can drive it instead of
        # fighting it for the controller.
        try:
            httpd, engine = serve(port=args.port)
        except OSError as e:
            print(f"cannot bind port {args.port}: {e}", file=sys.stderr)
            return 1
        except kbd.DeviceError as e:
            print(f"lightshow: {e}", file=sys.stderr)
            return 1
        print(f"LightShow daemon on 127.0.0.1:{args.port}")
        print("  device " + engine.kb.node)
        # systemd stops the service with SIGTERM; turn it into a clean exit so
        # the boards are closed (the KB7 must leave direct mode on the way out).
        import signal
        signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            engine.shutdown()
            try:
                engine.kb.close()
            except Exception as e:
                print(f"lightshow: closing boards failed: {e}", file=sys.stderr, flush=True)
        return 0

    if args.action == "gui":
        try:
            from lightshow.gui import run
        except Exception as e:
            print(f"GTK4 front end unavailable ({e}).\n"
                  f"Falling back to the web UI: lightshow serve", file=sys.stderr)
            return 1
        return run()

    if args.action != "serve":
        print(f"unknown action '{args.action}'. Try: lightshow list", file=sys.stderr)
        return 2

    try:
        httpd, engine = serve(port=args.port)
    except OSError as e:
        print(f"cannot bind port {args.port}: {e}", file=sys.stderr)
        return 1
    except kbd.DeviceError as e:
        print(f"lightshow: {e}", file=sys.stderr)
        return 1

    url = f"http://127.0.0.1:{args.port}/"
    print(f"LightShow for Omarchy  →  {url}")
    print(f"  device {engine.kb.node}   theme {kbd.theme_name()}")
    print("  Ctrl-C to stop (hardware effects keep running, software ones stop)")
    if not args.no_browser:
        open_browser(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        engine.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
