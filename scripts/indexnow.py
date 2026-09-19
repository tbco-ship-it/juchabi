#!/usr/bin/env python3
"""Submit all sitemap URLs to IndexNow (Bing/Yandex/Naver/Seznam share the index)."""
import json, re, sys, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOST = "juchabi.com"
KEY = (ROOT / "static/indexnow-key.txt").read_text().strip()
urls = re.findall(r"<loc>([^<]+)</loc>", (ROOT / "dist/sitemap.xml").read_text())
if len(sys.argv) > 1:
    urls = [u for u in urls if any(p in u for p in sys.argv[1:])]
for i in range(0, len(urls), 10000):
    body = json.dumps({"host": HOST, "key": KEY, "keyLocation": f"https://{HOST}/{KEY}.txt", "urlList": urls[i:i+10000]}).encode()
    req = urllib.request.Request("https://api.indexnow.org/indexnow", data=body, headers={"Content-Type": "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            print("indexnow", r.status, len(urls[i:i+10000]), "urls")
    except urllib.error.HTTPError as e:
        print("indexnow error", e.code, e.read().decode()[:300]); sys.exit(1)
