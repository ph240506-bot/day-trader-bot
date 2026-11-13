#!/bin/bash
# Send one command to OpenD's remote-command port (e.g. the phone verification code on first login).
exec 3<>/dev/tcp/127.0.0.1/22222 || { echo "OpenD isn't listening for commands yet"; exit 1; }
printf '%s\r\n' "$*" >&3
timeout 4 cat <&3
exec 3>&-
