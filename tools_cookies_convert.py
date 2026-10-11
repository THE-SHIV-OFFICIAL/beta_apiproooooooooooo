"""Convert a browser-export cookies.json into a safe YouTube-only cookies.txt.
Usage: python tools_cookies_convert.py cookies.json cookies/account1.txt
Only .youtube.com / .google.com cookies are kept — never ship other site cookies."""
import json, sys
KEEP = (".youtube.com", "youtube.com", ".google.com", "accounts.google.com")
src, dst = sys.argv[1], sys.argv[2]
rows = ["# Netscape HTTP Cookie File"]
for c in json.load(open(src)):
    d = c.get("domain", "")
    if not d.endswith(KEEP) and d not in KEEP:
        continue
    exp = int(c.get("expirationDate") or 0)
    rows.append("\t".join([d, "TRUE" if d.startswith(".") else "FALSE", c.get("path", "/"),
                           "TRUE" if c.get("secure") else "FALSE", str(exp), c["name"], c["value"]]))
open(dst, "w").write("\n".join(rows) + "\n")
print(f"wrote {len(rows)-1} cookies -> {dst}")
