#!/bin/sh
# Starts the command-line OpenD with the login from .env. Uses only tools already in the base image.
if [ -z "$MOOMOO_LOGIN" ] || [ -z "$MOOMOO_PASSWORD" ]; then
  echo "MOOMOO_LOGIN / MOOMOO_PASSWORD are not set in .env"; sleep 300; exit 1
fi
cd /opt/opend || { echo "/opt/opend missing"; sleep 300; exit 1; }
MD5=$(printf '%s' "$MOOMOO_PASSWORD" | md5sum | cut -d' ' -f1)
echo "Starting OpenD (Intel program under emulation: the first start is slow)"
exec ./OpenD -login_account="$MOOMOO_LOGIN" -login_pwd_md5="$MD5" -api_ip=0.0.0.0 -api_port=11111 -lang=en
