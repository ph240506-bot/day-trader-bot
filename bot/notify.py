"""Phone alerts through ntfy.sh: free, no account, install the ntfy app and subscribe to your topic.

Anyone who knows the topic name can read the alerts, so use a long random one
(the setup command in the README makes one) and keep it in a GitHub secret.
"""
import logging
import urllib.request

log = logging.getLogger("notify")


class Notifier:
    def __init__(self, cfg):
        self.topic = cfg.ntfy_topic
        self.server = cfg.ntfy_server.rstrip("/")
        self.levels = set(cfg.notify_on)

    def send(self, kind: str, title: str, message: str, priority: int = 3, tags: str = ""):
        """kind: trade | veto | near_miss | alert. Only kinds listed in NOTIFY_ON are sent."""
        if not self.topic or kind not in self.levels:
            return
        headers = {"Title": title.encode("ascii", "ignore").decode(), "Priority": str(priority)}
        if tags:
            headers["Tags"] = tags
        try:
            req = urllib.request.Request(f"{self.server}/{self.topic}", data=message.encode("utf-8"),
                                         headers=headers, method="POST")
            urllib.request.urlopen(req, timeout=5).read()
        except Exception as e:  # an alert failing must never stop trading
            log.warning("notification failed: %s", e)
