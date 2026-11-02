#!/usr/bin/env bash
# One-time setup for running the bot 24/7 with Docker (Raspberry Pi / Linux).
# Re-run any time to change keys; your freellmapi data and trade journal are kept.
set -euo pipefail
cd "$(dirname "$0")/.."

command -v docker >/dev/null || { echo "Docker isn't installed. Run: curl -fsSL https://get.docker.com | sh"; exit 1; }
docker info >/dev/null 2>&1 || { echo "Docker needs permission. Run: sudo usermod -aG docker \$USER  then log out and back in."; exit 1; }

[ -f .env ] || cp .env.example .env
chmod 600 .env

# set_env NAME VALUE  -> writes NAME=VALUE into .env (replacing any existing line)
set_env() {
  python3 - "$1" "$2" <<'PY'
import re, sys
name, value = sys.argv[1], sys.argv[2]
lines = open(".env").read().splitlines()
out, done = [], False
for line in lines:
    if re.match(rf"\s*{re.escape(name)}\s*=", line):
        if not done:
            out.append(f"{name}={value}")
            done = True
    else:
        out.append(line)
if not done:
    out.append(f"{name}={value}")
open(".env", "w").write("\n".join(out) + "\n")
PY
}
get_env() { python3 -c "
import re,sys
for l in open('.env'):
    m=re.match(r'\s*$1\s*=\s*([^#\s]*)', l)
    if m: print(m.group(1)); break"; }

ask() {  # ask NAME "description" [optional]
  local cur; cur="$(get_env "$1")"
  if [ -n "$cur" ]; then
    printf "%s is already set. Press Enter to keep it, or paste a new one: " "$1"
  else
    printf "Paste %s (%s) then press Enter%s: " "$1" "$2" "${3:+, or just Enter to skip}"
  fi
  local v; read -rs v; echo
  [ -n "$v" ] && set_env "$1" "$v"
  return 0
}

echo "== Keys (nothing shows while you paste; that's normal) =="
ask ALPACA_API_KEY "Alpaca paper Key, starts PK"
ask ALPACA_SECRET_KEY "Alpaca paper Secret"
ask GROQ_API_KEY "starts gsk_" optional
ask GEMINI_API_KEY "starts AQ. or AIza" optional
ask NTFY_TOPIC "your ntfy topic, e.g. pat-bot-..." optional

echo
echo "== More free AI providers (optional; more = faster answers and fewer limits) =="
ask CEREBRAS_API_KEY "cloud.cerebras.ai, very fast" optional
ask OPENROUTER_API_KEY "openrouter.ai/keys" optional
ask MISTRAL_API_KEY "console.mistral.ai" optional
ask NVIDIA_API_KEY "build.nvidia.com" optional

echo
echo "== Small-cap mode (optional; needs a SECOND Alpaca paper account) =="
ask SMALLCAP_ALPACA_API_KEY "second paper account Key, PK..." optional
ask SMALLCAP_ALPACA_SECRET_KEY "second paper account Secret" optional
ask FINNHUB_API_KEY "finnhub.io free key, extra price check" optional
ask SEC_CONTACT_EMAIL "your email: the SEC asks bots reading its filings to identify themselves" optional
if [ -n "$(get_env SMALLCAP_ALPACA_API_KEY)" ] && [ -n "$(get_env SMALLCAP_ALPACA_SECRET_KEY)" ]; then
  set_env COMPOSE_PROFILES smallcap
fi

[ -n "$(get_env ALPACA_API_KEY)" ] && [ -n "$(get_env ALPACA_SECRET_KEY)" ] || { echo "Alpaca keys are required."; exit 1; }

# Settings that match your GitHub setup
grep -q '^MAX_OPEN_POSITIONS=4' .env && set_env MAX_OPEN_POSITIONS 20
grep -q '^MAX_POSITION_PCT=10' .env && set_env MAX_POSITION_PCT 5
[ -n "$(get_env FREELLMAPI_ENCRYPTION_KEY)" ] || set_env FREELLMAPI_ENCRYPTION_KEY "$(openssl rand -hex 32)"
[ -n "$(get_env FREELLMAPI_ADMIN_PASSWORD)" ] || set_env FREELLMAPI_ADMIN_PASSWORD "$(openssl rand -hex 16)"
mkdir -p journal

ADMIN_EMAIL=bot@localhost.test
ADMIN_PW="$(get_env FREELLMAPI_ADMIN_PASSWORD)"

echo "== Starting freellmapi =="
export FREEAPI_CONFIG_JSON
FREEAPI_CONFIG_JSON="$(GROQ_API_KEY="$(get_env GROQ_API_KEY)" GEMINI_API_KEY="$(get_env GEMINI_API_KEY)" \
  CEREBRAS_API_KEY="$(get_env CEREBRAS_API_KEY)" MISTRAL_API_KEY="$(get_env MISTRAL_API_KEY)" \
  OPENROUTER_API_KEY="$(get_env OPENROUTER_API_KEY)" NVIDIA_API_KEY="$(get_env NVIDIA_API_KEY)" \
  python3 scripts/build_freellmapi_config.py "$ADMIN_EMAIL" "$ADMIN_PW")"
docker compose up -d freellmapi
KEY="$(python3 scripts/freellmapi_key.py http://localhost:3001 "$ADMIN_EMAIL" "$ADMIN_PW")"
set_env LLM_API_KEY "$KEY"
unset FREEAPI_CONFIG_JSON

echo "== Building and starting the bot (first build on a Pi takes a few minutes) =="
docker compose up -d --build
sleep 15
docker compose logs --tail 5 bot
if grep -q '^COMPOSE_PROFILES=smallcap' .env; then
  echo "-- small-cap mode --"
  docker compose logs --tail 5 smallcap
fi
cat <<'MSG'

Done. The bot now runs 24/7 and restarts itself after reboots.
  Live log:      docker compose logs -f bot      (Ctrl+C stops watching, not the bot)
  Trade log:     journal/trades.csv
  Stop it:       docker compose stop bot
  Update code:   git pull && docker compose up -d --build bot

IMPORTANT: turn off the GitHub schedule so two bots don't trade the same account:
  gh workflow disable day-trader      (run on your Mac)
MSG
