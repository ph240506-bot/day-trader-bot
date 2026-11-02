#!/bin/bash
# Pi watchdog: phone alert if the Pi overheats, loses power quality, a bot stops, or the disk fills up.
# Runs every 5 minutes from cron. Each alert repeats at most once an hour while the problem lasts.
cd "$HOME/day-trader-bot" || exit 0
TOPIC=$(grep -E '^NTFY_TOPIC=' .env | head -1 | cut -d= -f2 | awk '{print $1}')
[ -z "$TOPIC" ] && exit 0
STATE="$HOME/.pi_watchdog"; mkdir -p "$STATE"

alert() {  # alert KEY TITLE MESSAGE PRIORITY
  local f="$STATE/$1"
  if [ -f "$f" ] && [ $(( $(date +%s) - $(stat -c %Y "$f") )) -lt 3600 ]; then return; fi
  touch "$f"
  curl -s -m 10 -H "Title: $2" -H "Priority: $4" -H "Tags: warning" -d "$3" "https://ntfy.sh/$TOPIC" >/dev/null
}
ok() { rm -f "$STATE/$1"; }

# --- temperature ---
TEMP=$(vcgencmd measure_temp | grep -oE '[0-9]+(\.[0-9]+)?' | head -1)
T=${TEMP%.*}
if [ "${T:-0}" -ge 80 ]; then
  alert hot "Pi is HOT: ${TEMP}C" "Over 80C, it's slowing itself down. Give it more air or add a fan." 5
elif [ "${T:-0}" -ge 72 ]; then
  alert warm "Pi running warm: ${TEMP}C" "Over 72C. Fine for now, but check it isn't boxed in or in the sun." 3
else
  ok hot; ok warm
fi

# --- power / throttling (bit 0 = under-voltage now, bit 2 = throttled now) ---
THR=$(vcgencmd get_throttled | cut -d= -f2)
if (( THR & 0x1 )); then alert power "Pi power problem" "Under-voltage right now. Use the official 27W USB-C power supply." 4; else ok power; fi
if (( THR & 0x4 )); then alert throttle "Pi is throttling" "It has slowed itself down (heat or power). Temp: ${TEMP}C." 4; else ok throttle; fi

# --- bot services (skipped while paused with botpanic) ---
if [ ! -f "$STATE/paused" ]; then
  RUNNING=$(docker compose ps --status running --services 2>/dev/null)
  SERVICES="bot freellmapi"
  grep -q '^COMPOSE_PROFILES=smallcap' .env && SERVICES="$SERVICES smallcap"
  for s in $SERVICES; do
    if grep -qx "$s" <<<"$RUNNING"; then ok "down_$s"
    else alert "down_$s" "Bot service down: $s" "Restart it with: cd ~/day-trader-bot && docker compose up -d" 4; fi
  done
fi

# --- disk space ---
DISK=$(df --output=pcent / | tail -1 | tr -dc 0-9)
if [ "${DISK:-0}" -ge 90 ]; then alert disk "Pi disk ${DISK}% full" "Free space with: docker system prune -f" 4; else ok disk; fi
