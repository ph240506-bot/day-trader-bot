#!/bin/sh
# Starts the command-line OpenD with the login from .env. Uses only tools already in the base image.
# The password is kept in .env only as its MD5 fingerprint (32 plain letters/digits). A raw password with
# characters like $ # " or \ gets mangled on its way through .env, and then every login is refused.
MD5="$MOOMOO_PASSWORD_MD5"
[ -z "$MD5" ] && [ -n "$MOOMOO_PASSWORD" ] && MD5=$(printf '%s' "$MOOMOO_PASSWORD" | md5sum | cut -d' ' -f1)
if [ -z "$MOOMOO_LOGIN" ] || [ -z "$MD5" ]; then
  echo "moomoo login is not set in .env: run  bash ~/day-trader-bot/scripts/moomoo_setup.sh"; sleep 300; exit 1
fi
cd /opt/opend || { echo "/opt/opend missing"; sleep 300; exit 1; }
echo "Starting OpenD (Intel program under emulation: the first start is slow)"
exec ./OpenD -login_account="$MOOMOO_LOGIN" -login_pwd_md5="$MD5" -api_ip=0.0.0.0 -api_port=11111 -lang=en
