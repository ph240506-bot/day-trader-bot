#!/bin/bash
# Installs the terminal shortcuts and the 5-minute watchdog (heat / power / bot-down / disk alerts).
# Safe to run again: it never adds anything twice.
set -e
cd "$(dirname "$0")/.."
chmod +x scripts/pi_watchdog.sh
LINE='[ -f ~/day-trader-bot/scripts/pi_shortcuts.sh ] && . ~/day-trader-bot/scripts/pi_shortcuts.sh'
grep -qF "$LINE" ~/.bashrc 2>/dev/null || echo "$LINE" >> ~/.bashrc
CURRENT=$(crontab -l 2>/dev/null | grep -v pi_watchdog.sh || true)      # keep any other cron jobs
printf '%s\n%s\n' "$CURRENT" "*/5 * * * * $HOME/day-trader-bot/scripts/pi_watchdog.sh" | sed '/^$/d' | crontab -
TOPIC=$(grep -E '^NTFY_TOPIC=' .env 2>/dev/null | head -1 | cut -d= -f2 | awk '{print $1}')
if [ -n "$TOPIC" ]; then
  curl -s -m 10 -H "Title: Pi watchdog installed" -H "Tags: white_check_mark" \
    -d "You'll get an alert if the Pi passes 72C, has power problems, a bot stops, or the disk fills up." \
    "https://ntfy.sh/$TOPIC" >/dev/null && echo "Test alert sent to your phone."
fi
echo "Installed. Run:  source ~/.bashrc   then  bothelp"
