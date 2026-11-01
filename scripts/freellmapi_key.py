"""Wait for a freshly started freellmapi, log in with the admin account from the
declarative config, and print the unified API key the bot uses.

Usage: python scripts/freellmapi_key.py http://localhost:3001 admin@example.com <password>
"""
import json
import sys
import time
import urllib.request


def call(url, data=None, token=None):
    req = urllib.request.Request(url, data=json.dumps(data).encode() if data else None,
                                 headers={"Content-Type": "application/json",
                                          **({"Authorization": f"Bearer {token}"} if token else {})})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read() or b"{}")


def main(base, email, password):
    for _ in range(60):  # up to ~2 min for the container to boot
        try:
            urllib.request.urlopen(f"{base}/api/ping", timeout=5)
            break
        except Exception:
            time.sleep(2)
    else:
        sys.exit("freellmapi never came up")
    token = call(f"{base}/api/auth/login", {"email": email, "password": password})["token"]
    print(call(f"{base}/api/settings/api-key", token=token)["apiKey"])


if __name__ == "__main__":
    main(*sys.argv[1:4])
