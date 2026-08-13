#!/usr/bin/env bash
# Installs a per-user LaunchAgent that starts local Orchestra at login.
#
# RunAtLoad only — deliberately no KeepAlive: `local.sh stop` must mean
# stopped, and a crashing server should stay down for inspection rather than
# flap. launchd owns the process tree, so the server survives whichever
# shell, agent session, or terminal performed the install.
#
# Idempotent: re-running replaces the agent (and restarts the job, which
# no-ops when the server is already running). Run from the checkout that
# should serve — the plist bakes in this repo's absolute path.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="ai.unify.orchestra-local"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/.unity/orchestra-login-start.log"

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/.unity"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>$ROOT/scripts/login_start.sh</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>AbandonProcessGroup</key>
  <true/>
  <key>StandardOutPath</key>
  <string>$LOG</string>
  <key>StandardErrorPath</key>
  <string>$LOG</string>
</dict>
</plist>
EOF

uid="$(id -u)"
launchctl bootout "gui/$uid/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$uid" "$PLIST"

echo "Installed $LABEL for $ROOT (RunAtLoad only, no KeepAlive)."
echo "Logs: $LOG"
echo "Manual start without a fresh login: launchctl kickstart gui/$uid/$LABEL"
