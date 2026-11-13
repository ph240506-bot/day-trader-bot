# ===== trading bot shortcuts =====
BOTDIR=~/day-trader-bot

# --- checking ---
alias botcheck='cd $BOTDIR && docker compose ps --format "{{.Service}}: {{.Status}}" && echo && docker compose logs --tail 2 bot && docker compose logs --tail 2 smallcap 2>/dev/null; echo; vcgencmd measure_temp; vcgencmd get_throttled; df -h / | tail -1 | awk "{print \"disk used: \" \$5}"'
alias botlog='cd $BOTDIR && docker compose logs -f --tail 30 bot'
alias sclog='cd $BOTDIR && docker compose logs -f --tail 30 smallcap'
alias bottrades='tail -20 $BOTDIR/journal/trades.csv'
alias sctrades='tail -20 $BOTDIR/journal/smallcap.csv'
alias boterrors='cd $BOTDIR && docker compose logs --since 24h bot smallcap 2>/dev/null | grep -iE "error|failed|halt|loss limit" | tail -30'
alias bottemp='watch -n 2 vcgencmd measure_temp'

# --- control ---
alias botrestart='cd $BOTDIR && docker compose up -d --force-recreate bot'   # re-reads .env
alias screstart='cd $BOTDIR && docker compose up -d --force-recreate smallcap'   # re-reads .env
alias botupdate='cd $BOTDIR && git pull && docker compose up -d --build && echo && echo "Now running version $(cat VERSION 2>/dev/null || echo unknown)"'
alias botversion='cat $BOTDIR/VERSION 2>/dev/null || echo unknown'
botreport() { cd $BOTDIR && docker compose exec -T -e DAYS="${DAYS:-1}" bot python -m bot.report; }
botbacktest() { cd $BOTDIR && docker compose exec -T -e DAYS="${DAYS:-90}" bot python -m bot.backtest; }
# --- moomoo live prices (optional; set up with: bash ~/day-trader-bot/scripts/moomoo_setup.sh) ---
alias moomoolog='cd $BOTDIR && docker compose logs -f --tail 40 opend'
moomoocode() { cd $BOTDIR && docker compose exec -T opend /usr/local/bin/send.sh input_phone_verify_code -code="$1"; }
moomoopic()  { cd $BOTDIR && docker compose exec -T opend /usr/local/bin/send.sh input_pic_verify_code -code="$1"; }
moomootest() { cd $BOTDIR && docker compose exec -T -e SC_MOOMOO=true smallcap python -m bot.smallcap.moomoo_test "$@"; }
moomooon()  { cd $BOTDIR && sed -i '/^SC_MOOMOO=/d' .env && echo "SC_MOOMOO=true" >> .env && docker compose up -d --force-recreate smallcap && echo "moomoo prices ON"; }
moomoooff() { cd $BOTDIR && sed -i '/^SC_MOOMOO=/d' .env && echo "SC_MOOMOO=false" >> .env && docker compose up -d --force-recreate smallcap && echo "moomoo prices OFF"; }
alias botexport='cd $BOTDIR && docker compose exec -T bot python - < scripts/export_fills.py'

# --- safety ---
botpanic() {
  # EMERGENCY: stop both bots and close every position in both paper accounts.
  read -r -p "Stop both bots and close ALL their positions? Type YES: " a
  [ "$a" = "YES" ] || { echo "Cancelled."; return; }
  cd $BOTDIR || return
  mkdir -p ~/.pi_watchdog && touch ~/.pi_watchdog/paused
  docker compose stop bot smallcap 2>/dev/null
  docker compose run --rm --no-deps -T bot python -c "from bot.config import Config; from alpaca.trading.client import TradingClient as T; c=Config(); print(len(T(c.alpaca_key, c.alpaca_secret, paper=c.paper).close_all_positions(cancel_orders=True)), 'main-bot positions closing')"
  if grep -q '^COMPOSE_PROFILES=smallcap' .env; then
    docker compose run --rm --no-deps -T smallcap python -c "from bot.smallcap.config import SCConfig; from alpaca.trading.client import TradingClient as T; c=SCConfig(); print(len(T(c.alpaca_key, c.alpaca_secret, paper=c.paper).close_all_positions(cancel_orders=True)), 'small-cap positions closing')"
  fi
  docker compose run --rm --no-deps -T bot sh -c "rm -f journal/crypto_exits.json journal/smallcap_positions.json"
  echo "Done. Both bots stopped. Stocks sell at the next open if the market is shut. Run 'botresume' to start again."
}
botresume() {
  cd $BOTDIR && docker compose up -d && rm -f ~/.pi_watchdog/paused && echo "Bots running again."
}

bothelp() {
  cat <<'H'
botcheck    health check: services, latest status, temp, disk
botlog      main bot live log        sclog      small-cap live log
bottrades   main bot trade log       sctrades   small-cap trade log
boterrors   problems in the last 24h
bottemp     live temperature (Ctrl+C to stop)
botrestart  restart main bot         screstart  restart small-cap bot
botupdate   install an update (after pushing from your Mac)
botreport   performance report (DAYS=30 botreport for a month) - paste it to Claude
  moomoolog / moomoocode 123456 / moomootest / moomooon / moomoooff   moomoo live prices
  botbacktest compare ORB vs EMA on 90 days of real data, net of costs (~10 min; DAYS=180 botbacktest)
botexport   export all fills from Alpaca (for the monthly review)
botpanic    EMERGENCY: stop both bots + close every position
botresume   start the bots again after botpanic
H
}
# ===== end trading bot shortcuts =====
