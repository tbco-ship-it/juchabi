"""좌표 없는 주차장 주소 → VWorld 지오코더(국토부, 무료) → data/geocode_cache.json {addr: {lat, lng, matched, type}}.
응답은 _geo_ok(원주소 vs refined.text 전 구성요소 비교)를 통과해야만 status=ok로 저장; parcel이 거절되면 road로 재시도.
normalize.py가 status=ok 항목만 읽어 lat/lng가 비어 있는 lot을 채운다 (geo_source='vworld').

  VWORLD_API_KEY=... ../martday/.venv/bin/python scripts/geocode.py

지번(parcel) → 도로명(road) 순으로 시도; 실패한 주소도 캐시에 null로 남겨 재호출하지 않는다.
전남광주통합특별시(표준데이터 표기)는 VWorld가 모르므로 광주 5개 구는 광주광역시, 나머지는 전라남도로 바꿔 묻는다.
"""
import json, os, re, sys, time, urllib.parse, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from normalize import _geo_ok  # noqa: E402  (same guard as the consumer)
KEY = os.environ.get("VWORLD_API_KEY")
if not KEY:
    sys.exit("VWORLD_API_KEY missing")
CACHE = ROOT / "data/geocode_cache.json"
cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
GWANGJU_GU = {"동구", "서구", "남구", "북구", "광산구"}


def query_addr(addr):
    a = re.sub(r"\(.*?\)", "", addr).strip()
    a = re.sub(r"\s*(?:,.*|외\s*\d+\s*필지.*|일대|부근|일원|주변)$", "", a).strip()  # 첫 필지로 묻고, 페이지엔 대표 위치라고 적는다
    a = re.sub(r"번지$", "", a).strip()
    m = re.match(r"전남광주통합특별시\s+(\S+)", a)
    if m:
        a = a.replace("전남광주통합특별시", "광주광역시" if m.group(1) in GWANGJU_GU else "전라남도", 1)
    return a


VALIDATOR = 3  # bump when _geo_ok changes so rejected/null entries get re-queried


def geocode(addr):
    """Try parcel then road; accept only a hit that passes _geo_ok against the *original* address. Returns a cache entry."""
    q = query_addr(addr)
    last = None
    for typ in ("parcel", "road"):
        params = urllib.parse.urlencode({"service": "address", "request": "getcoord", "version": "2.0", "crs": "epsg:4326", "address": q,
                                         "refine": "true", "simple": "false", "format": "json", "type": typ, "key": KEY})
        try:
            with urllib.request.urlopen(f"https://api.vworld.kr/req/address?{params}", timeout=20) as r:
                res = json.load(r)["response"]
        except Exception as e:  # network blip: leave uncached so the next run retries
            print("ERR", addr, e)
            return "retry"
        if res.get("status") == "OK":
            pt = res["result"]["point"]
            hit = {"lat": round(float(pt["y"]), 6), "lng": round(float(pt["x"]), 6), "matched": res["refined"]["text"], "type": typ}
            if _geo_ok(addr, hit):
                return {**hit, "status": "ok", "validator": VALIDATOR}
            last = {**hit, "status": "rejected_address_mismatch", "validator": VALIDATOR}
        elif res.get("status") == "ERROR":
            last = last or {"status": "api_error:" + str(res.get("error", {}).get("code", "")), "validator": VALIDATOR}
    return last or {"status": "not_found", "validator": VALIDATOR}


lots = json.loads((ROOT / "data/lots.json").read_text())["lots"]
def stale(entry):
    """Re-query anything that has not passed the current validator: old-format hits, rejections and not-found from an older validator."""
    return not entry or entry.get("status") != "ok" or entry.get("validator") != VALIDATOR


# lots.json is the pre-geocode normalize output (apply_geocode fills lat only from status=ok entries), so "no lat" = needs a verified hit
todo = sorted(a for a in {l["addr"] for l in lots if not l["lat"] or l.get("geo_source") == "vworld"} if stale(cache.get(a)))
print(f"{len(todo)} addresses to geocode ({len(cache)} cached)")
ok = 0
for i, addr in enumerate(todo, 1):
    r = geocode(addr)
    if r == "retry":
        continue
    cache[addr] = r
    ok += r["status"] == "ok"
    if i % 50 == 0:
        CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=0))
        print(f"  {i}/{len(todo)} ok={ok}")
    time.sleep(0.05)
CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=0))
print(f"done: {ok}/{len(todo)} resolved; cache {len(cache)} entries, {sum(1 for v in cache.values() if v and v.get('status') == 'ok')} verified coords")
