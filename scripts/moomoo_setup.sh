#!/bin/bash
# One-time setup for moomoo live prices on the Pi. Safe to re-run.
#   bash ~/day-trader-bot/scripts/moomoo_setup.sh
set -e
cd ~/day-trader-bot
ENV=.env

ask() {  # ask VAR "prompt" [secret]
  local cur; cur=$(grep -E "^$1=" $ENV | head -1 | cut -d= -f2-)
  if [ -n "$cur" ]; then echo "$1 already set (delete that line in .env to change it)"; return; fi
  local val
  if [ "$3" = "secret" ]; then read -r -s -p "$2: " val; echo; else read -r -p "$2: " val; fi
  [ -n "$val" ] && printf '%s=%s\n' "$1" "$val" >> $ENV
}

echo "== moomoo live prices: setup =="
ask MOOMOO_LOGIN "moomoo ID, phone number or email you log in with"
ask MOOMOO_PASSWORD "moomoo login password (stays in .env on this Pi, never goes to GitHub)" secret
ask MOOMOO_OPEND_URL "The 'Download Ubuntu' link from moomoo's OpenAPI page (long-press the link, copy, paste)"

# Intel emulation: now, and again after every reboot
docker run --privileged --rm tonistiigi/binfmt --install amd64 >/dev/null
CRON='@reboot sleep 30 && docker run --privileged --rm tonistiigi/binfmt --install amd64 >/dev/null 2>&1 && cd ~/day-trader-bot && docker compose up -d opend >/dev/null 2>&1'
CUR=$(crontab -l 2>/dev/null || true)
echo "$CUR" | grep -q "tonistiigi/binfmt" || { echo "$CUR"; echo "$CRON"; } | crontab -

# switch the gateway on (prices stay OFF in the bot until SC_MOOMOO=true)
PROF=$(grep -E '^COMPOSE_PROFILES=' $ENV | cut -d= -f2-)
case ",$PROF," in *,moomoo,*) ;; *) sed -i '/^COMPOSE_PROFILES=/d' $ENV; echo "COMPOSE_PROFILES=${PROF:+$PROF,}moomoo" >> $ENV;; esac

echo "Building the gateway (first time takes several minutes on the Pi)..."
docker compose build opend
docker compose up -d opend
echo
echo "Started. Now run:   moomoolog"
echo "  - If it asks for a phone verification code, run:   moomoocode 123456   (your code)"
echo "  - When it says login succeeded, run:   moomootest"
