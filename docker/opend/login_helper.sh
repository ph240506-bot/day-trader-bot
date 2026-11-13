#!/bin/bash
# OpenD 10.x has no stored login: it asks "Please enter account" / "Please enter password" every start.
# This watches OpenD's own log for those prompts and answers them through its command port, the same
# way a person would type them. It answers each prompt once, and gives up after 3 refused logins so a
# wrong password can never hammer the account.
LOGDIR="${OPEND_LOGDIR:-/root/.com.moomoo.OpenD/Log}"
PORT="${OPEND_TELNET_PORT:-22222}"
PWFILE="${MOOMOO_PWFILE:-/run/secrets/moomoo_password}"
POLL="${OPEND_POLL:-3}"

send() {
  exec 3<>"/dev/tcp/127.0.0.1/$PORT" || return 1
  sleep 1
  printf '%s\r\n' "$1" >&3
  sleep 1
  exec 3>&-
}

handled=0 attempts=0 logfile=""
while true; do
  sleep "$POLL"
  newest=$(ls -t "$LOGDIR"/GTWLog_*.log 2>/dev/null | head -1)
  [ -z "$newest" ] && continue
  if [ "$newest" != "$logfile" ]; then logfile="$newest"; handled=0; fi
  # last prompt-ish line and its line number
  hit=$(grep -anE "strDescUTF8  =  (Please enter account|Please enter password|Login successful)" "$logfile" | tail -1)
  [ -z "$hit" ] && continue
  n=${hit%%:*}
  [ "$n" -le "$handled" ] && continue
  case "$hit" in
    *"Login successful"*)
      handled=$n; attempts=0 ;;
    *"Please enter account"*)
      if [ "$attempts" -ge 3 ]; then
        echo "[login helper] 3 logins were refused. Not trying again (to protect the account)."
        echo "[login helper] Fix the password with:  bash ~/day-trader-bot/scripts/moomoo_setup.sh --password"
        handled=$n; continue
      fi
      attempts=$((attempts + 1))
      echo "[login helper] answering the account prompt (attempt $attempts)"
      send "$MOOMOO_LOGIN" && handled=$n ;;
    *"Please enter password"*)
      if [ ! -s "$PWFILE" ]; then
        echo "[login helper] no saved password: run  bash ~/day-trader-bot/scripts/moomoo_setup.sh --password"
        handled=$n; continue
      fi
      echo "[login helper] answering the password prompt"
      send "$(cat "$PWFILE")" && handled=$n ;;
  esac
done
