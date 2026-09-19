"""공공데이터포털 활용신청 API → data/raw/<name>_<date>.json (키는 환경변수 DATAGO_API_KEY, 저장소에 넣지 않는다).

  DATAGO_API_KEY=... ../martday/.venv/bin/python scripts/fetch_datago.py [busan|daegu|all]

busan: 부산광역시_공영주차장 정보 조회 (15004683, 자동승인, 615행) — 공영 615곳, 요금(tenMin/pkBascTime/feeAdd/ftDay/ftMon)·좌표
daegu: 대구광역시_부설주차장운영및개방공유정보조회서비스 (15108762, 자동승인) — 9면 이상 건축물 부설주차장, 개방공유 참여여부(shp_yn)
"""
import datetime as dt, json, os, sys, urllib.parse, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KEY = os.environ.get("DATAGO_API_KEY")
if not KEY:
    sys.exit("DATAGO_API_KEY missing")
today = dt.date.today()

SOURCES = {
    "busan": ("https://apis.data.go.kr/6260000/BusanPblcPrkngInfoService/getPblcPrkngInfo", {"resultType": "json"}, "부산광역시_공영주차장 정보 조회 (data.go.kr 15004683)"),
    "daegu": ("https://apis.data.go.kr/6270000/dgBuildingPark/getBuildingParkList", {"type": "json"}, "대구광역시_부설주차장운영및개방공유정보조회서비스 (data.go.kr 15108762)"),
}


def pull(url, extra):
    rows, page = [], 1
    while True:
        q = urllib.parse.urlencode({"serviceKey": KEY, "pageNo": page, "numOfRows": 500, **extra})
        with urllib.request.urlopen(f"{url}?{q}", timeout=90) as r:
            body = json.load(r)
        if "response" in body:
            body = body["response"]
        hdr, b = body["header"], body["body"]
        if hdr["resultCode"] != "00":
            sys.exit(f"{url}: {hdr}")
        items = b["items"]["item"] if b.get("items") else []
        if isinstance(items, dict):
            items = [items]
        rows += items
        if not items or len(rows) >= int(b["totalCount"]):
            return rows, int(b["totalCount"])
        page += 1


for name in (sys.argv[1:] or ["all"]):
    for key in (SOURCES if name == "all" else [name]):
        url, extra, label = SOURCES[key]
        rows, total = pull(url, extra)
        out = ROOT / f"data/raw/{key}_{today.strftime('%Y%m%d')}.json"
        out.write_text(json.dumps({"source": label, "fetched": today.isoformat(), "rows": rows}, ensure_ascii=False))
        print(f"{key}: {len(rows)}/{total} rows -> {out.relative_to(ROOT)}")
