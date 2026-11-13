"""Download moomoo OpenD, pull out the command-line program's folder, and pre-set its config.
Runs natively at build time (see Dockerfile).  usage: fetch.py <url> <out_dir>"""
import os
import shutil
import subprocess
import sys
import tarfile
import urllib.request

url, out = sys.argv[1], sys.argv[2]
if not url:
    sys.exit("MOOMOO_OPEND_URL is not set in .env (the 'Ubuntu' link from moomoo's OpenD download page)")
print("downloading", url, flush=True)
req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
with urllib.request.urlopen(req, timeout=300) as r, open("/tmp/opend.tgz", "wb") as f:
    shutil.copyfileobj(r, f)
print("size: %.1f MB" % (os.path.getsize("/tmp/opend.tgz") / 1e6), flush=True)
with tarfile.open("/tmp/opend.tgz") as t:
    t.extractall("/tmp/x")

# the archive holds a GUI build and a command-line build; we want the folder with the plain "OpenD" program
found = []
for root, _, files in os.walk("/tmp/x"):
    if "OpenD" in files:
        found.append(root)
found.sort(key=lambda p: ("gui" in p.lower(), len(p)))
if not found:
    for root, dirs, files in os.walk("/tmp/x"):
        print(root, files[:8])
    sys.exit("no 'OpenD' program found in the download")
src = found[0]
print("command-line OpenD folder:", src, "->", sorted(os.listdir(src))[:20], flush=True)
shutil.copytree(src, out, symlinks=True)
os.chmod(os.path.join(out, "OpenD"), 0o755)
xml = os.path.join(out, "OpenD.xml")
if os.path.exists(xml):
    subprocess.run([sys.executable, os.path.join(os.path.dirname(__file__), "patch_xml.py"), xml,
                    "telnet_ip=127.0.0.1", "telnet_port=22222"], check=True)
else:
    print("WARNING: no OpenD.xml in that folder", flush=True)
