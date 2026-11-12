"""Set key=value items in OpenD.xml whatever its exact layout: replace, un-comment, or add."""
import re
import sys

path, pairs = sys.argv[1], dict(a.split("=", 1) for a in sys.argv[2:])
s = open(path, encoding="utf-8", errors="ignore").read()
for k, v in pairs.items():
    live = re.compile(rf"(?<!<!--)(?<!<!-- )<{k}>.*?</{k}>", re.S)
    commented = re.compile(rf"<!--\s*<{k}>.*?</{k}>\s*-->", re.S)
    new = f"<{k}>{v}</{k}>"
    if commented.search(s):
        s = commented.sub(new, s, count=1)
    elif live.search(s):
        s = live.sub(new, s, count=1)
    else:
        i = s.rfind("</")
        s = s[:i] + f"\t{new}\n" + s[i:] if i >= 0 else s + new
open(path, "w", encoding="utf-8").write(s)
