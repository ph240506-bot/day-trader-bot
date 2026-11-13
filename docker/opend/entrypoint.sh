#!/bin/bash
# Starts the command-line OpenD and a small helper that answers its login prompts. Uses only tools
# already in the base image (nothing can be installed under emulation).
if [ -z "$MOOMOO_LOGIN" ]; then
  echo "MOOMOO_LOGIN is not set in .env: run  bash ~/day-trader-bot/scripts/moomoo_setup.sh"; sleep 300; exit 1
fi
cd /opt/opend || { echo "/opt/opend missing"; sleep 300; exit 1; }
echo "Starting OpenD (Intel program under emulation: the first start is slow)"
/usr/local/bin/login_helper.sh &
exec ./OpenD -api_ip=0.0.0.0 -api_port=11111 -lang=en
