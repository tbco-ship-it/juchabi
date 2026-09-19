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


def _hhmm4(v):
    v = (v or "").strip()
    return f"{v[:2]}:{v[2:]}" if re.fullmatch(r"\d{4}", v) else ""


def _nk(name):
    """'압구정 428 공영주차장(구)' → '압구정428' — the key the Seoul API and the 구청 rows share."""
    s = re.sub(r"\((구|시|공단|시설공단|무인|건축물식)\)", "", name or "")
    s = re.sub(r"공영주차장|공영|주차장|노상|노외|관광버스전용|유료|무인", "", s)
    s = re.sub(r"초교", "초", s)
    return re.sub(r"[\s()（）\-·,.]", "", s)


def _loose_match(key, gu, pool, taken):
    """Fallback for name variants ('청암' ↔ '청암(무인)', '화곡6동 방죽길' ↔ '방죽길'): one side's key contains the other's,
    the candidate is in the same 구, not yet matched, and the containment is unique — otherwise no match."""
    if len(key) < 2:
        return None
    hits = [l for l in pool if l["sigungu"] == gu and id(l) not in taken
            and len(_nk(l["name"])) >= 2 and (key in _nk(l["name"]) or _nk(l["name"]) in key)]
    return hits[0] if len(hits) == 1 else None


def _addr_key(addr):
    """'강남구 압구정동 428-0' / '서울특별시 강남구 압구정동 428(…)' → ('강남구', '압구정동', '428'); 723-8 stays '723-8' (723-7 next door is another lot)."""
    a = re.sub(r"\(.*?\)", "", addr or "").replace("서울특별시", "").strip()
    m = re.search(r"(\S+구)\s+(\S+[동가로])\s+(\d+)(?:-(\d+))?", a)
    if not m:
        return None
    sub = m.group(4) or "0"
    return (m.group(1), m.group(2), m.group(3) if sub == "0" else f"{m.group(3)}-{sub}")


def _free_flag(code):
    """Y=유료 / N=무료 per the API's own *_NM companions; anything else is unknown (None), never silently '유료'."""
    return {"N": True, "Y": False}.get(str(code or "").strip().upper())


def merge_seoul(lots):
    """서울열린데이터광장 GetParkInfo (data/raw/seoul_parkinfo_*.json): 848 public lots, most already in the standard set.
    A code attaches to a standard lot only when the evidence is unambiguous (one name candidate, or one 지번 candidate, and no
    name-vs-address conflict); a loose name containment counts only when unique. Codes with several candidates or a conflict go to
    data/wip/seoul_review.json instead of becoming duplicates. Unmatched 시간제 lots are added as new lots (id seoul-<code>);
    extra 노상 sections of the same name fold into one lot (spaces summed), 노외 name twins go to review."""
    files = sorted((ROOT / "data/raw").glob("seoul_parkinfo_*.json"))
    if not files:
        return
    rows = json.loads(files[-1].read_text())["rows"]
    fetched = files[-1].stem.split("_")[-1]
    by_code = {}
    for r in rows:
        by_code.setdefault(r["PKLT_CD"], r)
    seoul = [l for l in lots if l["sido"] == "서울특별시"]
    by_name = defaultdict(list)
    by_addr = defaultdict(list)
    for l in seoul:
        by_name[(l["sigungu"], _nk(l["name"]))].append(l)
        for k in {_addr_key(a) for a in (l.get("addr_parcel"), l["addr"]) if a} - {None}:
            by_addr[k].append(l)
    matched = loose = added = sections = 0
    review = []
    taken = set()
    new_by_key = {}
    for r in by_code.values():
        gu = (r["ADDR"] or "").split()[0] if r.get("ADDR") else ""
        nk = _nk(r["PKLT_NM"])
        name_c = by_name.get((gu, nk), [])
        addr_c = by_addr.get(_addr_key(r["ADDR"]) or ("", "", ""), [])
        how = None
        if len(name_c) == 1 and (not addr_c or name_c[0] in addr_c):
            cands, how = name_c, "name"
        elif len(name_c) == 1 and addr_c and name_c[0] not in addr_c:
            cands, how = [], "conflict"  # 이름은 A, 지번은 B — 자동 병합 금지
        elif len(name_c) > 1:
            cands, how = [], "ambiguous_name"
        elif len(addr_c) == 1:
            cands, how = addr_c, "parcel"
        elif len(addr_c) > 1:
            cands, how = [], "ambiguous_parcel"
        else:
            hit = _loose_match(nk, gu, seoul, taken)
            cands, how = ([hit], "loose") if hit else ([], None)
            loose += bool(hit)
        if how in ("conflict", "ambiguous_name", "ambiguous_parcel"):
            review.append({"code": r["PKLT_CD"], "name": r["PKLT_NM"], "addr": r["ADDR"], "why": how,
                           "candidates": [{"id": c["id"], "name": c["name"], "addr": c["addr"]} for c in name_c + addr_c]})
            continue
        sync = (r.get("LAST_DATA_SYNC_TM") or "")[:10]
        extra = {"sat_free": _free_flag(r.get("SAT_CHGD_FREE_SE")), "hol_free": _free_flag(r.get("LHLDY_YN")), "night_free": r.get("NGHT_FREE_OPN_YN") == "Y",
                 "day_max_won": int(r["DLY_MAX_CRG"]) if (r.get("DLY_MAX_CRG") or 0) > 0 else None,
                 "seoul_code": r["PKLT_CD"], "seoul_match": how, "seoul_oper": r.get("OPER_SE_NM") or "",
                 "seoul_realtime": r.get("PRK_NOW_INFO_PVSN_YN") in ("1", "2"), "seoul_sync": sync}
        if cands:
            l = cands[0]
            if l.get("seoul_code"):
                # 노상 구간마다 코드가 따로 온다 (망원 11개) — 첫 구간이 비워 둔 값만 뒷 구간에서 채운다
                if not l.get("day_max_won") and extra["day_max_won"]:
                    l["day_max_won"] = extra["day_max_won"]
                if not l["month_won"] and to_int(r.get("MNTL_CMUT_CRG")):
                    l["month_won"] = to_int(r.get("MNTL_CMUT_CRG"))
                l.setdefault("seoul_codes", [l["seoul_code"]]).append(r["PKLT_CD"])
                continue
            taken.add(id(l))
            l.update(extra)
            if not l["month_won"] and to_int(r.get("MNTL_CMUT_CRG")):
                l["month_won"] = to_int(r.get("MNTL_CMUT_CRG"))
            matched += 1
            continue
        if r.get("OPER_SE_NM") in ("버스전용 주차장", "거주자 우선 주차장"):
            review.append({"code": r["PKLT_CD"], "name": r["PKLT_NM"], "addr": r["ADDR"], "why": "not_hourly:" + r["OPER_SE_NM"], "candidates": []})
            continue  # 일반 승용차가 시간제로 쓸 수 없는 곳은 신규 lot으로 만들지 않는다
        nkey = (gu, nk)
        if nkey in new_by_key:
            prev = new_by_key[nkey]
            if r.get("PKLT_KND") == "NS" and prev["type"] == "노상":
                if (r.get("TPKCT") or 0) > 0:
                    prev["spaces"] = (prev["spaces"] or 0) + int(r["TPKCT"])
                prev.setdefault("seoul_codes", [prev["seoul_code"]]).append(r["PKLT_CD"])
                sections += 1
            else:
                review.append({"code": r["PKLT_CD"], "name": r["PKLT_NM"], "addr": r["ADDR"], "why": "name_twin_offstreet", "candidates": [{"id": prev["id"], "name": prev["name"], "addr": prev["addr"]}]})
            continue
        try:
            lat, lng = float(r.get("LAT") or 0), float(r.get("LOT") or 0)
            if not (33 <= lat <= 39 and 124 <= lng <= 132):
                lat = lng = None
        except ValueError:
            lat = lng = None
        m = re.match(r"^(\S+구)", r["ADDR"] or "")
        if not m:
            review.append({"code": r["PKLT_CD"], "name": r["PKLT_NM"], "addr": r["ADDR"], "why": "no_gu_in_addr", "candidates": []})
            continue
        paid = r.get("CHGD_FREE_SE") != "N"
        basic_won = int(r["PRK_CRG"]) if paid and (r.get("PRK_CRG") or 0) > 0 else None
        basic_min = int(r["PRK_HM"]) if paid and (r.get("PRK_HM") or 0) > 0 else None
        add_won = int(r["ADD_CRG"]) if paid and (r.get("ADD_CRG") or 0) > 0 else None
        add_min = int(r["ADD_UNIT_TM_MNT"]) if paid and (r.get("ADD_UNIT_TM_MNT") or 0) > 0 else None
        notes = []
        if extra["sat_free"]: notes.append("토요일 무료")
        if extra["hol_free"]: notes.append("공휴일 무료")
        if extra["night_free"]: notes.append("야간 무료개방")
        new_by_key[nkey] = {
            "id": f"seoul-{r['PKLT_CD']}", "name": re.sub(r"\s*\((구|시)\)$", "", re.sub(r"\s+", " ", r["PKLT_NM"]).strip()), "kind": "공영",
            "type": "노상" if r.get("PKLT_KND") == "NS" or "노상" in (r.get("PKLT_KND_NM") or "") else "노외",
            "addr": "서울특별시 " + (r["ADDR"] or "").strip(), "addr_parcel": "서울특별시 " + (r["ADDR"] or "").strip(), "sido": "서울특별시", "sido_slug": SIDO["서울특별시"][0], "sigungu": m.group(1), "gu": "",
            "spaces": int(r["TPKCT"]) if (r.get("TPKCT") or 0) > 0 else None, "grade": "", "rotation": "",
            "oper_day": "평일+토요일+공휴일",
            "hours": {"weekday": [_hhmm4(r.get("WD_OPER_BGNG_TM")), _hhmm4(r.get("WD_OPER_END_TM"))], "sat": [_hhmm4(r.get("WE_OPER_BGNG_TM")), _hhmm4(r.get("WE_OPER_END_TM"))], "holiday": [_hhmm4(r.get("LHLDY_BGNG")), _hhmm4(r.get("LHLDY"))]},
            "fee": "유료" if paid else "무료", "basic_min": basic_min, "basic_won": basic_won, "add_min": add_min, "add_won": add_won,
            "day_hours": None, "day_won": None, "month_won": to_int(r.get("MNTL_CMUT_CRG")),
            "hourly_review": False, "daily_review": False, "monthly_review": False, "fee_review": False, "fee_raw": {},
            "pay": "", "note": " / ".join(notes), "discounts": {}, "free_open": "야간 무료개방" if extra["night_free"] else "",
            "org": "서울시설공단" if "(시)" in r["PKLT_NM"] else f"서울특별시 {m.group(1)}", "phone": (r.get("TELNO") or "").strip(), "lat": lat, "lng": lng,
            "disabled_zone": False, "ref_date": sync or fetched, "provider": "서울열린데이터광장 (서울시 공영주차장 안내 정보)",
            **extra,
        }
        lots.append(new_by_key[nkey])
        added += 1
    (ROOT / "data/wip").mkdir(exist_ok=True)
    (ROOT / "data/wip/seoul_review.json").write_text(json.dumps(review, ensure_ascii=False, indent=1))
    print(f"seoul merge: {matched} lots matched (attributes added; {loose} via loose name containment), {added} new lots "
          f"({sections} extra 노상 sections folded into them), {len(review)} codes to data/wip/seoul_review.json from {len(by_code)} unique codes")


_GEO_ADDR = re.compile(r"(?P<prefix>.+?)\s+(?P<name>[^\s(),]+(?:동|리|가|로|길))\s*(?P<mountain>산\s*)?(?P<main>\d+)(?:\s*-\s*(?P<sub>\d+))?(?:번지)?")
_PREFIX_ALIAS = {"광주광역시": "전남광주통합특별시", "전라남도": "전남광주통합특별시", "서울": "서울특별시", "부산": "부산광역시", "대구": "대구광역시", "인천": "인천광역시"}


_APPROX = re.compile(r"\s*(?:,.*|외\s*\d+\s*필지.*|일대|부근|일원|주변)$")


def _geo_addr_key(text):
    """('parcel'|'road', prefix tokens, 동·리·가/로·길 name, 산 여부, 본번, 부번) — the whole string must parse, else None.
    '계림동 268 외 6필지', '대인동 16-13 일대', '부기리 557-7, 557-9' parse as their first parcel (see _geo_approx for the flag)."""
    if not isinstance(text, str):
        return None
    text = re.sub(r"\s+", " ", re.sub(r"\([^)]*\)", "", text)).strip()
    text = _APPROX.sub("", text)
    m = _GEO_ADDR.fullmatch(text)
    if not m:
        return None
    prefix = tuple(_PREFIX_ALIAS.get(t, t) for t in m["prefix"].split())
    return ("road" if m["name"].endswith(("로", "길")) else "parcel", prefix, m["name"], bool(m["mountain"]), int(m["main"]), int(m["sub"] or 0))


def _geo_approx(addr):
    """True when the source address names an area or several parcels — the coordinate is a representative point, not the entrance."""
    return bool(_APPROX.search(re.sub(r"\([^)]*\)", "", addr or "").strip()))


def _geo_ok(addr, hit):
    """Accept a VWorld hit only when every address component agrees: same kind, same 동·리·가 (or 로·길), same 산 flag, same 본번·부번,
    and the requested 시도·시군구 tokens all appear in the returned prefix (VWorld may add the 읍·면 the source omitted).
    refine=true otherwise happily returns 영등포동4가 53-2 for 영등포동2가 53-0, or 방학동 산 58-10 for 방학동 58-10."""
    req, ret = _geo_addr_key(addr), _geo_addr_key(hit.get("matched", "") if isinstance(hit, dict) else "")
    if not req or not ret or req[0] != ret[0] or req[2:] != ret[2:]:
        return False
    if not set(req[1]) <= set(ret[1]):
        return False
    try:
        lat, lng = float(hit["lat"]), float(hit["lng"])
    except (KeyError, TypeError, ValueError):
        return False
    return 33 <= lat <= 39 and 124 <= lng <= 132


def apply_geocode(lots):
    """scripts/geocode.py가 만든 data/geocode_cache.json으로 좌표 없는 lot을 채운다 (geo_source='vworld')."""
    path = ROOT / "data/geocode_cache.json"
    if not path.exists():
        return
    cache = json.loads(path.read_text())
    filled = rejected = 0
    for l in lots:
        if l["lat"]:
            continue
        hit = cache.get(l["addr"])
        if not hit or "lat" not in hit:
            continue
        if hit.get("status", "ok") != "ok" or not _geo_ok(l["addr"], hit):
            rejected += 1
            continue
        l["lat"], l["lng"], l["geo_source"], l["geo_approx"] = hit["lat"], hit["lng"], "vworld", _geo_approx(l["addr"])
        filled += 1
    print(f"geocode: {filled} lots got VWorld coordinates, {rejected} fuzzy matches rejected, {sum(1 for l in lots if not l['lat'])} still without")


_SAT_FREE = re.compile(r"(토(?:요일)?\s*[·,+/]?\s*(?:일(?:요일)?)?\s*[·,+/]?\s*(?:공휴일)?\s*(?:은|는)?\s*무료|주말\s*(?:및\s*공휴일\s*)?(?:은|는)?\s*무료|토[·,+/]공휴일\s*무료|토·일·공휴일\s*무료)")
_HOL_FREE = re.compile(r"((?:토(?:요일)?\s*[·,+/]\s*)?(?:일(?:요일)?\s*[·,+/]\s*)?공휴일\s*(?:은|는)?\s*무료|주말\s*및\s*공휴일\s*무료|토[·,+/]공휴일\s*무료|주말[·,+/]공휴일\s*무료)")


def weekend_flags(lots):
    """Outside Seoul the standard data has no 토·공휴일 유무료 column. Two legal inferences, each labelled with its basis:
    - 'note': the 지자체 비고 says so in words ('토+공휴일 무료', '주말 및 공휴일 무료') → sat_free/hol_free True, basis 'note'.
    - 'oper_day': 운영요일 lacks 토요일/공휴일 → no fee collection runs that day; for 노상 that is a free-parking signal
      (spaces stay on the street), for 노외 the gate may be shut → sat_unstaffed/hol_unstaffed True, shown with that caveat."""
    n_note = n_oper = 0
    for l in lots:
        if l.get("seoul_code") or l["fee"] == "무료":
            continue
        note = l.get("note") or ""
        if _SAT_FREE.search(note) or _HOL_FREE.search(note):
            if _SAT_FREE.search(note):
                l["sat_free"] = True
            if _HOL_FREE.search(note):
                l["hol_free"] = True
            l["weekend_basis"] = "note"
            n_note += 1
        od = l.get("oper_day") or ""
        if od and "토요일" not in od:
            l["sat_unstaffed"] = True
        if od and "공휴일" not in od:
            l["hol_unstaffed"] = True
        if l.get("sat_unstaffed") or l.get("hol_unstaffed"):
            l.setdefault("weekend_basis", "oper_day")
            n_oper += 1
    print(f"weekend flags: {n_note} lots from 비고 text, {n_oper} lots with 토/공휴일 outside 운영요일")


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
            "addr": addr, "addr_parcel": (r["LNMADR"] or "").strip(), "sido": sido, "sido_slug": SIDO[sido][0], "sigungu": sigungu, "gu": gu,
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
    merge_seoul(lots)
    apply_geocode(lots)
    weekend_flags(lots)
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
