#!/usr/bin/env python3
"""data/raw/parking_*.json (전국주차장정보표준데이터, data.go.kr 15012896) -> data/lots.json
One record per 주차장. Fees stay as the provider reported them; 감면 rules are parsed out of 특기사항 (SPCMNT)
and kept next to the raw sentence so the page can show both."""
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 광역 단위. 2026-07 전남·광주 통합 → 제공기관명은 이미 '전남광주통합특별시', 주소 문자열은 옛 이름이 섞여 있다.
SIDO = {
    "서울특별시": ("seoul", "서울"), "부산광역시": ("busan", "부산"), "대구광역시": ("daegu", "대구"), "인천광역시": ("incheon", "인천"),
    "전남광주통합특별시": ("gwangju-jeonnam", "전남·광주"), "대전광역시": ("daejeon", "대전"), "울산광역시": ("ulsan", "울산"),
    "세종특별자치시": ("sejong", "세종"), "경기도": ("gyeonggi", "경기"), "강원특별자치도": ("gangwon", "강원"),
    "충청북도": ("chungbuk", "충북"), "충청남도": ("chungnam", "충남"), "전북특별자치도": ("jeonbuk", "전북"),
    "경상북도": ("gyeongbuk", "경북"), "경상남도": ("gyeongnam", "경남"), "제주특별자치도": ("jeju", "제주"),
}
ALIAS = {"전라남도": "전남광주통합특별시", "광주광역시": "전남광주통합특별시", "전븍특별자치도": "전북특별자치도", "전라북도": "전북특별자치도",
         "강원도": "강원특별자치도", "제주도": "제주특별자치도", "세종시": "세종특별자치시"}
GWANGJU_GU = {"동구", "서구", "남구", "북구", "광산구"}

CATS = [  # (key, label, regex)
    ("light", "경차", r"경차|경형\s*자동차|경형"),
    ("green", "저공해·전기차", r"저공해|친환경|전기\s*자동차|전기차|환경친화|수소"),
    ("disabled", "장애인", r"장애"),
    ("multi", "다자녀", r"다자녀|다둥이|두\s*자녀|세\s*자녀|다누리|아이조아|아이사랑|자녀"),
    ("veteran", "국가유공자", r"국가유공|보훈|유공자|고엽제|참전"),
    ("senior", "65세 이상", r"65세|경로|노인"),
    ("pregnant", "임산부", r"임산부|임신"),
    ("rotation", "요일제·부제", r"요일제|부제|함께타기|승용차\s*요일"),
]
PCT = re.compile(r"(\d{2,3})\s*(?:%|퍼센트|프로|할인|감면|경감|감경)")
CONDITIONAL = re.compile(r"\d+\s*(?:시간|분)\s*(?:면제|무료|이내|까지|초과)|면제\s*후|이후|초과\s*시|1일\s*1|회당|75세|70세")
FREE = re.compile(r"면제|무료")


def split_outside_parens(s):
    """Split on + / ; , only when not inside (...)."""
    out, depth, cur = [], 0, ""
    for ch in s:
        if ch in "(（": depth += 1
        elif ch in ")）": depth = max(0, depth - 1)
        if depth == 0 and ch in "+/;,":
            out.append(cur); cur = ""
        else:
            cur += ch
    out.append(cur)
    return [x.strip() for x in out if x.strip()]


def parse_discounts(note):
    """{'light': {'pct': 50, 'text': '경차 50프로 할인', 'conditional': False}, ...}; pct None = mentioned but not safely computable.
    A segment that carries several rates ('장애인 80%, 경차 50%'), a rate plus 면제, a conditional ('4시간 면제 후 50%') or a range
    is kept as text only — the calculator must not pick the first percentage for every category in the sentence."""
    found = {}
    if not note:
        return found
    for seg in split_outside_parens(note):
        rates = {int(m.group(1)) for m in PCT.finditer(seg)}
        pct = next(iter(rates)) if len(rates) == 1 else None
        if not rates and FREE.search(seg):
            pct = 100
        ambiguous = len(rates) > 1 or bool(rates and FREE.search(seg)) or bool(CONDITIONAL.search(seg)) or bool(re.search(r"\d+\s*[~∼–-]\s*\d+", seg))
        if ambiguous or pct is None or not 0 < pct <= 100:
            pct = None
        for key, _label, rx in CATS:
            if not re.search(rx, seg):
                continue
            old = found.get(key)
            text = seg if old is None else old["text"] + " / " + seg
            value = pct if old is None or old["pct"] == pct else None
            found[key] = {"pct": value, "text": text, "conditional": value is None}
    return found


def free_open(note):
    """Sentence like '무료개방(평일 야간+토·일·공휴일)' → short label, else ''."""
    if not note:
        return ""
    m = re.search(r"[^+/;,]*(?:무료\s*개방|무료\s*운영|무료개방|주말\s*무료|공휴일\s*무료|야간\s*무료|무료\s*\(?(?:토|일|공휴일|야간))[^+/;,]*", note)
    return m.group(0).strip()[:80] if m else ""


def hhmm(v):
    v = (v or "").strip()
    return v if re.fullmatch(r"\d{1,2}:\d{2}", v) else ""


INTEGER = re.compile(r"^(?:\d+|\d{1,3}(?:,\d{3})+)$")


def to_int(v):
    """Only plain integers. '150+300', '0.5', '4.5' are compound/decimal fees the provider typed in — they must not be
    digit-stripped into 150300 / 5; the caller marks the lot fee_review instead."""
    v = (v or "").strip()
    return int(v.replace(",", "")) if INTEGER.match(v) else None


def fee_reviews(r):
    """Compound fee text ('150+300', '0.5') blocks only the calculation that needs it: hourly, day ticket, monthly ticket."""
    groups = {"hourly_review": ("BASIC_TIME", "BASIC_CHARGE", "ADD_UNIT_TIME", "ADD_UNIT_CHARGE"), "daily_review": ("DAY_CMMTKT_ADJ_TIME", "DAY_CMMTKT"), "monthly_review": ("MONTH_CMMTKT",)}
    invalid = lambda k: bool((r.get(k) or "").strip()) and to_int(r.get(k)) is None
    checks = {name: any(invalid(k) for k in fields) for name, fields in groups.items()}
    fields = [k for group in groups.values() for k in group]
    return {**checks, "fee_review": any(checks.values()), "fee_raw": {k: r[k] for k in fields if invalid(k)}}


def slugify_ko(name):
    s = re.sub(r"[\s]+", "", name)
    s = re.sub(r"[()\[\]{}<>,./\\?!@#$%^&*'\"`:;|~=+·ㆍ]", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s or "주차장"


def main():
    raws = sorted((ROOT / "data/raw").glob("parking_*.json"))
    src = raws[-1]
    rows = sorted(json.loads(src.read_text())["rows"], key=lambda r: (r["REFERENCE_DATE"], r["INSTT_NM"].split()[0] in SIDO), reverse=True)
    lots, seen_id = [], set()
    dropped = Counter()
    for r in rows:
        addr = (r["RDNMADR"] or r["LNMADR"] or "").strip()
        if not addr:
            dropped["no_addr"] += 1; continue
        toks = addr.split()
        a0 = toks[0]
        inst0 = r["INSTT_NM"].split()[0]
        sido = inst0 if inst0 in SIDO else ALIAS.get(inst0, "")
        if not sido:
            sido = a0 if a0 in SIDO else ALIAS.get(a0, "")
        if not sido:  # '경기도동두천시평화로' 같은 붙여쓴 주소
            for k in list(SIDO) + list(ALIAS):
                if a0.startswith(k):
                    sido = k if k in SIDO else ALIAS[k]; toks = [k, a0[len(k):]] + toks[1:]; a0 = k; break
        if not sido:
            dropped["no_sido"] += 1; continue
        # 시군구: 주소 두 번째 토큰. 세종은 구가 없다. 통합시의 옛 광주 5개 구는 '광주 ○구'로 표기.
        if sido == "세종특별자치시":
            sigungu = "세종시"
        else:
            t1 = toks[1] if len(toks) > 1 else ""
            m = re.match(r"^(\S+?(?:시|군|구))", t1)
            sigungu = m.group(1) if m else ""
            if not sigungu:  # 주소가 '충청남도 홍성읍 …'처럼 시군구를 건너뛴 경우 → 제공기관명에서
                it = r["INSTT_NM"].split()
                sigungu = it[1] if len(it) > 1 and it[1].endswith(("시", "군", "구")) else (t1 or "기타")
            if sido == "전남광주통합특별시" and (a0 == "광주광역시" or sigungu in GWANGJU_GU):
                sigungu = f"광주 {sigungu}"
        gu = ""
        if len(toks) > 2 and re.fullmatch(r"\S+구", toks[2]) and sigungu.endswith("시"):
            gu = toks[2]
        note = (r["SPCMNT"] or "").strip()
        fee = r["PARKINGCHRGE_INFO"].strip()
        if fee == "유료+무": fee = "혼합"
        lat, lng = r["LATITUDE"].strip(), r["LONGITUDE"].strip()
        try:
            lat, lng = float(lat), float(lng)
            if not (33 <= lat <= 39 and 124 <= lng <= 132): lat = lng = None
        except ValueError:
            lat = lng = None
        pid = r["PRKPLCE_NO"].strip()
        # 같은 주차장이 시청 + 시설공단, 옛 도명 + 통합시명으로 두 번 오는 경우가 1,000건 넘는다 → 이름+주소로 합친다 (관리번호는 재사용되므로 키가 못 된다)
        a_norm = re.sub(r"\s+", "", addr)
        for old, new in ALIAS.items():
            a_norm = a_norm.replace(old, new)
        dkey = (re.sub(r"\s+", "", r["PRKPLCE_NM"]), a_norm)
        if dkey in seen_id:
            dropped["dup_name_addr"] += 1; continue
        seen_id.add(dkey)
        lots.append({
            "id": pid, "name": re.sub(r"\s+", " ", r["PRKPLCE_NM"]).strip(), "kind": r["PRKPLCE_SE"].strip(), "type": r["PRKPLCE_TYPE"].strip(),
            "addr": addr, "sido": sido, "sido_slug": SIDO[sido][0], "sigungu": sigungu, "gu": gu,
            "spaces": to_int(r["PRKCMPRT"]), "grade": r["FEEDING_SE"].strip(), "rotation": r["ENFORCE_SE"].strip(),
            "oper_day": r["OPER_DAY"].strip(),
            "hours": {"weekday": [hhmm(r["WEEKDAY_OPER_OPEN_HHMM"]), hhmm(r["WEEKDAY_OPER_COLSE_HHMM"])],
                      "sat": [hhmm(r["SAT_OPER_OPER_OPEN_HHMM"]), hhmm(r["SAT_OPER_CLOSE_HHMM"])],
                      "holiday": [hhmm(r["HOLIDAY_OPER_OPEN_HHMM"]), hhmm(r["HOLIDAY_CLOSE_OPEN_HHMM"])]},
            "fee": fee, "basic_min": to_int(r["BASIC_TIME"]), "basic_won": to_int(r["BASIC_CHARGE"]),
            "add_min": to_int(r["ADD_UNIT_TIME"]), "add_won": to_int(r["ADD_UNIT_CHARGE"]),
            "day_hours": to_int(r["DAY_CMMTKT_ADJ_TIME"]), "day_won": to_int(r["DAY_CMMTKT"]), "month_won": to_int(r["MONTH_CMMTKT"]),
            **fee_reviews(r),
            "pay": r["METPAY"].strip(), "note": note, "discounts": parse_discounts(note), "free_open": free_open(note),
            "org": r["INSTITUTION_NM"].strip(), "phone": r["PHONE_NUMBER"].strip(), "lat": lat, "lng": lng,
            "disabled_zone": r["PWDBS_PPK_ZONE_YN"].strip() == "Y", "ref_date": r["REFERENCE_DATE"].strip(), "provider": r["INSTT_NM"].strip(),
        })
    # slugs unique within 시군구
    by_key = defaultdict(list)
    for l in lots:
        by_key[(l["sido_slug"], l["sigungu"])].append(l)
    for (ss, sg), lst in by_key.items():
        used = Counter()
        for l in sorted(lst, key=lambda x: x["id"]):
            base = slugify_ko(l["name"])
            used[base] += 1
            l["slug"] = base if used[base] == 1 else f"{base}-{used[base]}"
            l["path"] = f"{ss}/{sg}/{l['slug']}/"
    out = {"source": {"name": "전국주차장정보표준데이터", "url": "https://www.data.go.kr/data/15012896/standard.do", "file": src.name,
                      "fetched": src.stem.split("_")[1], "rows": len(rows)},
           "lots": lots}
    (ROOT / "data/lots.json").write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")))
    print(f"{len(lots)} lots from {len(rows)} rows; dropped {dict(dropped)}; sido {Counter(l['sido'] for l in lots).most_common()}")
    print("discount keys:", Counter(k for l in lots for k in l["discounts"]).most_common())
    print("free_open:", sum(1 for l in lots if l["free_open"]))


if __name__ == "__main__":
    main()
