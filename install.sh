#!/usr/bin/env bash
# LightShow installer.
#
# Installs to the user's home; the only thing needing root is the udev rule
# that grants access to the LED controller.
#
#   ./install.sh            install
#   ./install.sh --uninstall
#   ./install.sh --no-udev  skip the udev rule (run LightShow with sudo instead)

set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="$HOME/.local/bin"
APPS="$HOME/.local/share/applications"
ICONS="$HOME/.local/share/icons/hicolor/scalable/apps"
UNITS="$HOME/.config/systemd/user"
UDEV=/etc/udev/rules.d/99-msi-mysticlight.rules
UDEV_KB7=/etc/udev/rules.d/99-turtle-beach-kb7.rules
DESKTOP="$APPS/com.nixfred.LightShow.desktop"
LAUNCHER="$BIN/lightshow"

say()  { printf '  %s\n' "$*"; }
warn() { printf '  !! %s\n' "$*" >&2; }

uninstall() {
  if systemctl --user list-unit-files lightshow.service >/dev/null 2>&1; then
    systemctl --user disable --now lightshow.service 2>/dev/null || true
    rm -f "$UNITS/lightshow.service"
    systemctl --user daemon-reload 2>/dev/null || true
    say "removed the background service"
  fi
  rm -f "$LAUNCHER" "$DESKTOP" "$ICONS/com.nixfred.LightShow.svg"
  say "removed launcher, desktop entry and icon"
  if [[ -f $UDEV || -f $UDEV_KB7 ]]; then
    sudo rm -f "$UDEV" "$UDEV_KB7" && sudo udevadm control --reload-rules
    say "removed udev rules"
  fi
  say "config left alone at ~/.config/omarchy/lightshow.json"
  exit 0
}

[[ ${1:-} == --uninstall ]] && uninstall

echo "LightShow installer"

# -- dependencies -------------------------------------------------------
python3 - <<'PY' || { warn "Python 3.9+ is required"; exit 1; }
import sys
raise SystemExit(0 if sys.version_info >= (3, 9) else 1)
PY
say "python3 $(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])') ok"

if python3 -c 'import gi; gi.require_version("Gtk","4.0"); gi.require_version("Adw","1")' 2>/dev/null; then
  say "GTK4 + libadwaita found - the desktop app will work"
else
  warn "GTK4 / libadwaita not found. The desktop app will not start."
  warn "Install them (Arch: python-gobject gtk4 libadwaita) or use 'lightshow serve'."
fi

# -- launcher -----------------------------------------------------------
mkdir -p "$BIN" "$APPS" "$ICONS"
ln -sf "$SRC/lightshow.py" "$LAUNCHER"
chmod +x "$SRC/lightshow.py"
say "launcher  -> $LAUNCHER"

case ":$PATH:" in
  *":$BIN:"*) ;;
  *) warn "$BIN is not on your PATH - add it to use 'lightshow' from a shell";;
esac

# -- desktop entry ------------------------------------------------------
# Generated rather than shipped, so no absolute path is baked into the repo.
install -m644 "$SRC/packaging/com.nixfred.LightShow.svg" \
              "$ICONS/com.nixfred.LightShow.svg"
sed "s|@EXEC@|$LAUNCHER|g" "$SRC/packaging/com.nixfred.LightShow.desktop.in" \
  > "$DESKTOP"
command -v update-desktop-database >/dev/null && update-desktop-database "$APPS" 2>/dev/null || true
command -v gtk-update-icon-cache  >/dev/null && gtk-update-icon-cache -f -t "$HOME/.local/share/icons/hicolor" 2>/dev/null || true
say "desktop entry -> $DESKTOP"

# -- udev ---------------------------------------------------------------
if [[ ${1:-} == --no-udev ]]; then
  say "skipping udev rule as asked"
else
  echo
  say "The LED controller lives at /dev/hidraw*, which is root-only by default."
  say "The rule below grants the 'wheel' group access to MSI controllers only."
  say "That node is the LED microcontroller, NOT the key input device, so"
  say "write access to it cannot be used to read keystrokes."
  echo
  if sudo install -m644 "$SRC/packaging/99-msi-mysticlight.rules" "$UDEV"; then
    sudo udevadm control --reload-rules
    sudo udevadm trigger --subsystem-match=hidraw
    say "udev rule installed -> $UDEV"
    if ! id -nG | grep -qw wheel; then
      warn "you are not in the 'wheel' group; either join it (and re-login)"
      warn "or edit $UDEV to use a group you are in"
    fi
  else
    warn "could not install the udev rule; run LightShow with sudo instead"
  fi
  # Turtle Beach KB7: only the control interface, for the logged-in seat.
  if sudo install -m644 "$SRC/packaging/99-turtle-beach-kb7.rules" "$UDEV_KB7"; then
    sudo udevadm control --reload-rules
    sudo udevadm trigger --subsystem-match=hidraw
    say "udev rule installed -> $UDEV_KB7 (KB7 control interface only)"
  else
    warn "could not install the KB7 udev rule"
  fi
fi

# -- background service -------------------------------------------------
# Effects that animate across the zones are stepped from userspace, so
# something must stay alive for them to run. The service also keeps the
# keyboard following the desktop theme without the window being open.
if command -v systemctl >/dev/null && [[ -d /run/systemd/system || -n ${XDG_RUNTIME_DIR:-} ]]; then
  mkdir -p "$UNITS"
  sed "s|@EXEC@|$LAUNCHER|g" "$SRC/packaging/lightshow.service" \
    > "$UNITS/lightshow.service"
  systemctl --user daemon-reload 2>/dev/null || true
  if systemctl --user enable --now lightshow.service 2>/dev/null; then
    say "background service enabled (starts at login)"
  else
    warn "could not enable the user service; run 'lightshow daemon' yourself"
  fi
else
  warn "no systemd user session; run 'lightshow daemon' to keep effects alive"
fi

# -- verify -------------------------------------------------------------
echo
if "$LAUNCHER" status 2>/dev/null | grep -q 'NOT FOUND\|no MysticLight'; then
  warn "no MysticLight controller detected on this machine."
  warn "If you have one that reports a different name, point at it directly:"
  warn "    LIGHTSHOW_DEVICE=/dev/hidrawN lightshow"
else
  "$LAUNCHER" status
fi

echo
say "Done. Run 'lightshow', or find LightShow in your app launcher."
