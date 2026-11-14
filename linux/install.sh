#!/bin/bash
# FT8 Hunter for Linux: run ./install.sh from the unpacked download.
# Installs for you alone (no root): FT8 Hunter and WS (the ws-hunter build,
# an AppImage) go in ~/.local/opt/FT8-Hunter, a folder you own, so the
# hunter can update itself there. It adds menu entries and the commands
# ft8-hunter and ws-hunter in ~/.local/bin, and can start the hunter when you
# log in (its window) or run it as a systemd user service (no window).
# Running it again updates both. Settings and logs stay in
# ~/.local/share/FT8-Hunter, kept by an uninstall too.
#
#   ./install.sh               install or update (asks how to start, in a terminal)
#   ./install.sh --autostart   ...and start the hunter's window when you log in
#   ./install.sh --service     ...and run it as a systemd user service instead
#   ./install.sh --no-start    ...and neither (an update keeps what you chose before)
#   ./install.sh --uninstall   remove it (also: ~/.local/opt/FT8-Hunter/install.sh --uninstall)
set -e
APP=${FT8_HUNTER_DIR:-$HOME/.local/opt/FT8-Hunter}
BIN=$HOME/.local/bin
APPS=${XDG_DATA_HOME:-$HOME/.local/share}/applications
AUTO=${XDG_CONFIG_HOME:-$HOME/.config}/autostart
UNITS=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user
MARK="X-FT8-Hunter-Installer=1"   # in every file we write: only ours are replaced or removed
UNIT=ft8-hunter.service
ours() { [ -f "$1" ] && grep -q "FT8-Hunter-Installer" "$1"; }
have_systemd() { command -v systemctl > /dev/null && systemctl --user show-environment > /dev/null 2>&1; }

start=""
for a in "$@"; do
  case "$a" in
    --autostart) start=autostart ;;
    --service) start=service ;;
    --no-start) start=none ;;
    --uninstall) start=uninstall ;;
    -h|--help) sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option $a (see --help)"; exit 2 ;;
  esac
done

stop_service() {
  if ours "$UNITS/$UNIT" && have_systemd; then
    systemctl --user disable --now "$UNIT" > /dev/null 2>&1 || true
  fi
}

if [ "$start" = uninstall ]; then
  echo "Removing FT8 Hunter"
  stop_service
  for f in "$UNITS/$UNIT" "$AUTO/ft8-hunter.desktop" "$APPS/ft8-hunter.desktop" "$APPS/ft8-hunter-ws.desktop"; do
    ours "$f" && rm -f "$f" && echo "  $f"
  done
  have_systemd && systemctl --user daemon-reload || true
  for l in "$BIN/ft8-hunter" "$BIN/ws-hunter"; do
    [ -L "$l" ] && case "$(readlink "$l")" in "$APP"/*) rm -f "$l"; echo "  $l" ;; esac
  done
  if [ -d "$APP" ]; then
    pkill -f "^$APP/FT8-Hunter" 2>/dev/null || true
    rm -rf "$APP"; echo "  $APP"
  fi
  echo "Done. Your settings and logs are still in ${XDG_DATA_HOME:-$HOME/.local/share}/FT8-Hunter."
  exit 0
fi

cd "$(dirname "$(readlink -f "$0")")"
for f in FT8-Hunter ws-hunter.AppImage; do
  [ -f "$f" ] || { echo "$f isn't next to this script: unpack the whole download first"; exit 1; }
done
[ "$(uname -m)" = x86_64 ] || echo "Note: this build is for x86-64; this machine is $(uname -m)."

# How to start it: asked in a terminal on a first install; an update keeps the choice
if [ -z "$start" ]; then
  if ours "$UNITS/$UNIT"; then start=service
  elif ours "$AUTO/ft8-hunter.desktop"; then start=autostart
  elif [ -t 0 ]; then
    echo "Start FT8 Hunter when you log in?"
    echo "  1) yes, with its window (link, QR code, Quit)   [default]"
    echo "  2) yes, as a systemd user service (no window; open http://localhost:8080)"
    echo "  3) no, I'll start it from the menu"
    read -r -p "Choice [1]: " c
    case "$c" in 2) start=service ;; 3) start=none ;; *) start=autostart ;; esac
  else start=none
  fi
fi

echo "Installing FT8 Hunter and WS (ws-hunter) into $APP"
was_running=""
if ours "$UNITS/$UNIT" && have_systemd && systemctl --user is-active --quiet "$UNIT"; then
  was_running=service; systemctl --user stop "$UNIT"
fi
mkdir -p "$APP" "$BIN" "$APPS"
# Each file goes in by a rename: Linux allows that while the old one runs
for f in FT8-Hunter ws-hunter.AppImage README.md; do
  [ -f "$f" ] || continue
  if [ "$(readlink -f "$f")" != "$(readlink -f "$APP/$f" 2>/dev/null)" ]; then
    cp "$f" "$APP/$f.new"; mv -f "$APP/$f.new" "$APP/$f"
  fi
done
if [ "$(readlink -f "$0")" != "$(readlink -f "$APP/install.sh" 2>/dev/null)" ]; then
  cp "$0" "$APP/install.sh.new"; mv -f "$APP/install.sh.new" "$APP/install.sh"   # for --uninstall later
fi
chmod 755 "$APP/FT8-Hunter" "$APP/ws-hunter.AppImage" "$APP/install.sh"
ln -sfn "$APP/FT8-Hunter" "$BIN/ft8-hunter"
ln -sfn "$APP/ws-hunter.AppImage" "$BIN/ws-hunter"
echo "  $APP"
echo "  commands: ft8-hunter, ws-hunter (in $BIN)"

# AppImages need FUSE 2; without it, WS can still unpack itself and run
ws_exec="$APP/ws-hunter.AppImage"
if ! { { ldconfig -p || /sbin/ldconfig -p; } 2>/dev/null | grep -q 'libfuse\.so\.2'; }; then
  ws_exec="env APPIMAGE_EXTRACT_AND_RUN=1 $APP/ws-hunter.AppImage"
  fuse_note=1
fi

entry() {  # file name exec comment
  cat > "$1" <<DESKTOP
[Desktop Entry]
Type=Application
Name=$2
Comment=$4
Exec=$3
Icon=$5
Terminal=false
Categories=HamRadio;Network;
$MARK
DESKTOP
}
entry "$APPS/ft8-hunter.desktop" "FT8 Hunter" "$APP/FT8-Hunter --window" \
  "Finds and works FT8 stations with WS" network-wireless
echo "  menu: FT8 Hunter"
# Don't take over a WS menu entry you made yourself
if [ -f "$APPS/ws-hunter.desktop" ] && ! ours "$APPS/ws-hunter.desktop"; then
  echo "  menu: kept your own ws-hunter.desktop; this WS is the ws-hunter command"
  rm -f "$APPS/ft8-hunter-ws.desktop"
else
  entry "$APPS/ft8-hunter-ws.desktop" "WS (ws-hunter)" "$ws_exec" \
    "WS, the build FT8 Hunter controls" audio-card
  echo "  menu: WS (ws-hunter)"
fi
command -v update-desktop-database > /dev/null && update-desktop-database -q "$APPS" 2>/dev/null || true

case "$start" in
  autostart)
    stop_service; ours "$UNITS/$UNIT" && rm -f "$UNITS/$UNIT"
    mkdir -p "$AUTO"
    entry "$AUTO/ft8-hunter.desktop" "FT8 Hunter" "$APP/FT8-Hunter --window" \
      "Starts FT8 Hunter when you log in" network-wireless
    echo "  starts with its window when you log in" ;;
  service)
    ours "$AUTO/ft8-hunter.desktop" && rm -f "$AUTO/ft8-hunter.desktop"
    if [ -f "$UNITS/$UNIT" ] && ! ours "$UNITS/$UNIT"; then
      echo "  not touching your own $UNITS/$UNIT: point its ExecStart at $APP/FT8-Hunter --no-window"
    else
      mkdir -p "$UNITS"
      # Restart=on-failure: an update exits with 75 and systemd starts the new version
      cat > "$UNITS/$UNIT" <<UNITFILE
# $MARK
[Unit]
Description=FT8 Hunter (call, grid and ports: Settings in its web UI)
After=network-online.target

[Service]
ExecStart="$APP/FT8-Hunter" --no-window
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
UNITFILE
      if have_systemd; then
        systemctl --user daemon-reload
        systemctl --user enable --now "$UNIT" > /dev/null 2>&1 && was_running=""
        echo "  running as a systemd user service ($UNIT); to keep it running when you're logged out:"
        echo "    loginctl enable-linger $USER"
      else
        echo "  wrote $UNITS/$UNIT; no systemd user session here, so start it yourself"
      fi
    fi ;;
  none)
    if [ -n "$*" ]; then  # --no-start asked for: drop a start-up we added before
      stop_service; ours "$UNITS/$UNIT" && rm -f "$UNITS/$UNIT"
      ours "$AUTO/ft8-hunter.desktop" && rm -f "$AUTO/ft8-hunter.desktop"
    fi ;;
esac
[ "$was_running" = service ] && systemctl --user start "$UNIT"

case ":$PATH:" in *":$BIN:"*) ;; *) echo "Note: $BIN isn't on your PATH yet: log out and in, or use the menu entries.";; esac
[ -n "$fuse_note" ] && echo "Note: no libfuse2 here, so WS unpacks itself each start (slower). To run it directly: sudo apt install libfuse2 (Ubuntu 24.04: libfuse2t64)."
if ! id -nG | grep -qwE 'dialout|uucp'; then
  echo "Note: to reach the radio's serial port (CAT, FLRig), add yourself to the dialout group and log in again:"
  echo "    sudo usermod -aG dialout $USER"
fi
echo "In WS: Settings → Reporting, UDP server 127.0.0.1 port 2237."
if [ "$start" != service ] && pgrep -f "^$APP/FT8-Hunter" > /dev/null; then
  echo "FT8 Hunter is running the version from before: Quit it and start it again for this one."
elif [ "$start" != service ] && [ -n "$DISPLAY$WAYLAND_DISPLAY" ]; then
  echo "Starting FT8 Hunter."
  nohup "$APP/FT8-Hunter" --window > /dev/null 2>&1 &
fi
