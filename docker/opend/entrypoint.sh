#!/bin/sh
# Finds the command-line OpenD inside whatever folder layout the download has, points its remote-command
# port at this container only, and starts it with the login from .env.
set -e
if [ -z "$MOOMOO_LOGIN" ] || [ -z "$MOOMOO_PASSWORD" ]; then
  echo "MOOMOO_LOGIN / MOOMOO_PASSWORD are not set in .env"; sleep 300; exit 1
fi
BIN=$(find /opt/opend -type f -name OpenD ! -path '*GUI*' | head -1)
[ -n "$BIN" ] || BIN=$(find /opt/opend -type f -name 'OpenD*' -perm -u+x | head -1)
if [ -z "$BIN" ]; then echo "OpenD program not found in the download:"; find /opt/opend -maxdepth 3 | head -40; sleep 300; exit 1; fi
DIR=$(dirname "$BIN"); cd "$DIR"; chmod +x "$BIN"
[ -f OpenD.xml ] && python3 /usr/local/bin/patch_xml.py OpenD.xml telnet_ip=127.0.0.1 telnet_port=22222
MD5=$(printf '%s' "$MOOMOO_PASSWORD" | md5sum | cut -d' ' -f1)
echo "Starting OpenD from $DIR"
exec "$BIN" -login_account="$MOOMOO_LOGIN" -login_pwd_md5="$MD5" -api_ip=0.0.0.0 -api_port=11111 -lang=en
