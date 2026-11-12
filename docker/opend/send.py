"""Send one command to OpenD's remote-command port (e.g. the phone verification code on first login)."""
import socket
import sys
import time

cmd = " ".join(sys.argv[1:])
s = socket.create_connection(("127.0.0.1", 22222), timeout=10)
s.sendall((cmd + "\r\n").encode())
time.sleep(2)
s.settimeout(3)
try:
    print(s.recv(8192).decode(errors="ignore"))
except Exception:
    pass
s.close()
