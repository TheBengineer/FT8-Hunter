#!/bin/bash
# FT8 Hunter for macOS: double-click once (the first time: right-click → Open).
# Puts FT8 Hunter and WS (the ws-hunter build, as ws-hunter.app so a stock WS
# is left alone) in Applications, clears macOS's "downloaded from the
# internet" flag on them, and gives WS the shared memory it needs (WS's own
# com.wsjtx.sysctl, as its macOS notes describe; this asks for your password).
# Running it again updates both apps.
set -e
cd "$(dirname "$0")"
echo "Installing FT8 Hunter and WS (ws-hunter) into /Applications"
for pair in "FT8-Hunter.app:FT8 Hunter.app" "ws.app:ws-hunter.app"; do
  src=${pair%%:*} dst="/Applications/${pair#*:}"
  [ -d "$src" ] || { echo "$src isn't next to this script: unzip the whole download first"; exit 1; }
  rm -rf "$dst"
  ditto "$src" "$dst"
  xattr -dr com.apple.quarantine "$dst" 2>/dev/null || true
  echo "  $dst"
done

if [ "$(sysctl -n kern.sysv.shmmax)" -lt 52428800 ] || [ ! -f /Library/LaunchDaemons/com.wsjtx.sysctl.plist ]; then
  echo "WS needs more shared memory than macOS allows by default: your password, please"
  plist=/Library/LaunchDaemons/com.wsjtx.sysctl.plist
  sudo tee "$plist" > /dev/null <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.wsjtx.sysctl</string>
    <key>Program</key>
    <string>/usr/sbin/sysctl</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/sbin/sysctl</string>
        <string>kern.sysv.shmmax=52428800</string>
        <string>kern.sysv.shmall=25600</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
</dict>
</plist>
EOF
  sudo chown root:wheel "$plist"
  sudo chmod 644 "$plist"
  sudo sysctl kern.sysv.shmmax=52428800 kern.sysv.shmall=25600 > /dev/null ||  # now, without a restart
    echo "  (macOS didn't take it now: it will after a restart)"
  echo "  done. If System Settings → General → Login Items lists \"sysctl\", leave it on: it sets this at every start."
fi

echo "Starting FT8 Hunter. In WS: Settings → Reporting, UDP server 127.0.0.1 port 2237."
open "/Applications/FT8 Hunter.app"
