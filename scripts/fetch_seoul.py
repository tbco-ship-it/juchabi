"""서울열린데이터광장 → data/raw/seoul_parkinfo_<date>.json (+ seoul_parking_<date>.json 실시간 대상 목록).

  SEOUL_API_KEY=... ../martday/.venv/bin/python scripts/fetch_seoul.py

GetParkInfo: 서울시 공영주차장 안내 정보 (요금·토요일/공휴일 유료구분·1일 최대요금·월정기권, 노상 구간마다 한 행, 1,000행/호출).
GetParkingInfo: 실시간 주차대수 제공 주차장 목록. 키는 저장소에 넣지 않는다 (환경변수).
"""
import datetime as dt, json, os, sys, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KEY = os.environ.get("SEOUL_API_KEY")
if not KEY:
    sys.exit("SEOUL_API_KEY missing")
BASE = f"http://openapi.seoul.go.kr:8088/{KEY}/json"
today = dt.date.today().isoformat()


def pull(service):
    rows, start = [], 1
    while True:
        with urllib.request.urlopen(f"{BASE}/{service}/{start}/{start + 999}/", timeout=60) as r:
            body = json.load(r)
        if service not in body:
            sys.exit(f"{service}: {body}")
        total = body[service]["list_total_count"]
        rows += body[service]["row"]
        if start + 999 >= total:
            return rows
        start += 1000


for service, name in (("GetParkInfo", "parkinfo"), ("GetParkingInfo", "parking")):
    rows = pull(service)
    out = ROOT / f"data/raw/seoul_{name}_{today.replace('-', '')}.json"
    out.write_text(json.dumps({"source": f"서울열린데이터광장 {service}", "fetched": today, "rows": rows}, ensure_ascii=False))
    print(f"{service}: {len(rows)} rows -> {out.relative_to(ROOT)}")
