"""행정안전부 「전국 명절 무료주차장 현황」(data.go.kr 15099790, 공공누리 1유형) CSV → data/holiday_free.json.

Usage: python scripts/normalize_holiday.py <csv> --published YYYY-MM-DD [--url ...]
The file is republished before each 설/추석; rerun with the new CSV and the 명절 pages flip to the new holiday."""
import argparse
import csv
import io
import json
import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SIDO = {"서울": "seoul", "서울특별시": "seoul", "경기": "gyeonggi", "경기도": "gyeonggi", "인천": "incheon", "인천광역시": "incheon", "부산": "busan", "부산광역시": "busan",
        "대구": "daegu", "대구광역시": "daegu", "대전": "daejeon", "대전광역시": "daejeon", "광주": "gwangju-jeonnam", "광주광역시": "gwangju-jeonnam", "전남": "gwangju-jeonnam", "전라남도": "gwangju-jeonnam",
        "울산": "ulsan", "울산광역시": "ulsan", "세종": "sejong", "세종특별자치시": "sejong", "강원": "gangwon", "강원특별자치도": "gangwon", "강원도": "gangwon", "충북": "chungbuk", "충청북도": "chungbuk",
        "충남": "chungnam", "충청남도": "chungnam", "전북": "jeonbuk", "전북특별자치도": "jeonbuk", "전라북도": "jeonbuk", "경북": "gyeongbuk", "경상북도": "gyeongbuk", "경남": "gyeongnam", "경상남도": "gyeongnam",
        "제주": "jeju", "제주특별자치도": "jeju"}


def read(path):
    raw = Path(path).read_bytes()
    for enc in ("utf-8-sig", "cp949"):
        try:
            return list(csv.DictReader(io.StringIO(raw.decode(enc))))
        except UnicodeDecodeError:
            continue
    raise SystemExit("unknown encoding")


def hours(t):
    t = re.sub(r"\s+", "", t or "")
    return {"종일개방": "종일", "미개방": "", "": ""}.get(t, t.replace("~", "~"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv")
    ap.add_argument("--published", required=True)
    ap.add_argument("--url", default="https://www.data.go.kr/data/15099790/fileData.do")
    a = ap.parse_args()
    rows = read(a.csv)
    lots, skipped = [], Counter()
    for r in rows:
        toks = r["주소"].split()
        slug = SIDO.get(toks[0]) if toks else None
        if not slug:
            skipped["sido"] += 1
            continue
        sigungu = "세종시" if slug == "sejong" else (toks[1] if len(toks) > 1 else "")
        if not re.search(r"(시|군|구)$", sigungu):
            skipped["sigungu"] += 1
            continue
        days = []
        for i in range(1, 6):
            d, t = r.get(f"휴일_{i}_개방일", "").strip(), hours(r.get(f"휴일_{i}_개방시간"))
            if d and t:
                days.append([d, t])
        if not days:
            skipped["closed"] += 1  # listed but 미개방 on every day
            continue
        try:
            lat, lng = float(r["위도"]), float(r["경도"])
        except ValueError:
            lat = lng = None
        lots.append({"name": r["자원명"].strip(), "org": r["기관명"].strip(), "org_kind": r["관리기관구분"].strip(), "addr": r["주소"].strip(), "detail": r["상세주소"].strip(),
                     "lat": lat, "lng": lng, "type": r["주차장유형"].strip(), "spaces": int(r["주차면수"]) if r["주차면수"].strip().isdigit() else None,
                     "days": days, "note": re.sub(r"\s+", " ", r["참고사항"]).strip(), "sido_slug": slug, "sigungu": sigungu})
    first = rows[0]
    dates = sorted({d for l in lots for d, _ in l["days"]})
    out = {"source": {"year": first["연도"], "holiday": first["명절구분"], "dates": dates, "published": a.published, "url": a.url, "rows": len(rows)}, "lots": lots}
    (ROOT / "data/holiday_free.json").write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")))
    print(f"{len(lots)} lots ({first['연도']} {first['명절구분']}, {dates[0]}~{dates[-1]}), skipped {dict(skipped)}; sigungu {len({(l['sido_slug'], l['sigungu']) for l in lots})}")


if __name__ == "__main__":
    main()
