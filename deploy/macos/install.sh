#!/bin/sh
# Install (or reinstall) the two launchd agents that keep ThriftRadar running on this Mac: the backend
# (uvicorn on 127.0.0.1:8000) and the WhatsApp listener. Spec: DESIGN.md §4.1 (launchd KeepAlive,
# ThrottleInterval=300). Logs: ~/Library/Logs/ThriftRadar/. Uninstall: ./install.sh --uninstall
set -eu
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
AGENTS="$HOME/Library/LaunchAgents"
LOGS="$HOME/Library/Logs/ThriftRadar"
UV="$(command -v uv)"; NODE="$(command -v node)"
DOMAIN="gui/$(id -u)"
for name in backend listener; do
  launchctl bootout "$DOMAIN/com.thriftradar.$name" 2>/dev/null || true
done
if [ "${1:-}" = "--uninstall" ]; then
  rm -f "$AGENTS/com.thriftradar.backend.plist" "$AGENTS/com.thriftradar.listener.plist"
  echo "uninstalled"; exit 0
fi
mkdir -p "$AGENTS" "$LOGS"
cat > "$AGENTS/com.thriftradar.backend.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.thriftradar.backend</string>
  <key>ProgramArguments</key><array>
    <string>$UV</string><string>run</string><string>uvicorn</string><string>app.main:create_app</string>
    <string>--factory</string><string>--host</string><string>127.0.0.1</string><string>--port</string><string>8000</string>
  </array>
  <key>WorkingDirectory</key><string>$REPO/backend</string>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>$LOGS/backend.log</string>
  <key>StandardErrorPath</key><string>$LOGS/backend.log</string>
</dict></plist>
PLIST
cat > "$AGENTS/com.thriftradar.listener.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.thriftradar.listener</string>
  <key>ProgramArguments</key><array><string>$NODE</string><string>src/index.js</string></array>
  <key>WorkingDirectory</key><string>$REPO/listener</string>
  <key>RunAtLoad</key><true/>
  <!-- restart after a crash or a non-zero exit (with launchd's own 5-minute throttle); a clean stop stays stopped -->
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>ThrottleInterval</key><integer>300</integer>
  <key>StandardOutPath</key><string>$LOGS/listener.log</string>
  <key>StandardErrorPath</key><string>$LOGS/listener.log</string>
</dict></plist>
PLIST
for name in backend listener; do
  launchctl bootstrap "$DOMAIN" "$AGENTS/com.thriftradar.$name.plist"
done
echo "installed: com.thriftradar.backend and com.thriftradar.listener (logs in $LOGS)"
