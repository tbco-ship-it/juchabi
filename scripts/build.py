#!/usr/bin/env python3
"""Generate the static 주차비 site into dist/ from data/lots.json.
Pages: home · 16 시도 · 230 시군구 · one page per 주차장 · guides."""
import argparse
import datetime as dt
import hashlib
import json
import math
import re
import shutil
from xml.sax.saxutils import escape
from collections import defaultdict, Counter
from pathlib import Path
from statistics import median

from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
SITE = "주차비"
SIDO_ORDER = ["seoul", "gyeonggi", "incheon", "busan", "daegu", "daejeon", "gwangju-jeonnam", "ulsan", "sejong", "gangwon", "chungbuk", "chungnam", "jeonbuk", "gyeongbuk", "gyeongnam", "jeju"]
SIDO_SHORT = {"서울특별시": "서울", "부산광역시": "부산", "대구광역시": "대구", "인천광역시": "인천", "전남광주통합특별시": "전남·광주", "대전광역시": "대전", "울산광역시": "울산",
              "세종특별자치시": "세종", "경기도": "경기", "강원특별자치도": "강원", "충청북도": "충북", "충청남도": "충남", "전북특별자치도": "전북", "경상북도": "경북", "경상남도": "경남", "제주특별자치도": "제주"}
CAT_LABEL = {"light": "경차", "green": "저공해·전기차", "disabled": "장애인", "multi": "다자녀", "veteran": "국가유공자", "senior": "고령자", "pregnant": "임산부", "rotation": "요일제·부제"}
CAT_ORDER = ["light", "green", "disabled", "multi", "veteran", "senior", "pregnant", "rotation"]
HOURS = [1, 2, 3, 5, 8]


def won(n):
    if not isinstance(n, (int, float)) or isinstance(n, bool) or not math.isfinite(n):
        return "—"
    return (f"{n:,.0f}" if n == int(n) else f"{n:,.1f}") + "원"


def cost(l, minutes):
    """Fee for `minutes` under the reported 기본시간/기본요금/추가단위 rule; None when the lot has no usable rule."""
    if l["fee"] == "무료":
        return 0
    if l.get("hourly_review", l.get("fee_review", False)):
        return None  # 복합·소수 시간요금은 원문 보존, 계산 보류 (일·월권만 복합인 곳은 시간요금을 계산한다)
    b_min, b_won, a_min, a_won = l["basic_min"], l["basic_won"], l["add_min"], l["add_won"]
    if not b_won and not a_won:
        return None  # 유료인데 금액이 0/공란 → 미기재
    if b_won is None or (b_min is None and a_min is None):
        return None
    b_min = b_min or 0
    if minutes <= b_min:
        total = b_won
    elif a_min and a_won is not None:
        total = b_won + math.ceil((minutes - b_min) / a_min) * a_won
    elif a_min is None and a_won is None and b_min:
        return None  # 기본요금만 있고 초과 규칙이 없다
    else:
        return None
    if not l.get("daily_review", False) and l["day_won"] and (not l["day_hours"] or l["day_hours"] >= 24 or minutes <= l["day_hours"] * 60):
        total = min(total, l["day_won"])
    if l.get("day_max_won") and l["id"].startswith("seoul-"):
        total = min(total, l["day_max_won"])  # 서울시 API의 1일 최대요금 — 기본·추가요금도 같은 API에서 온 lot에만 상한 적용 (출처가 다른 요금표를 섞지 않는다)
    return total


def make_nearby(lots):
    """Straight-line neighbours within 2 km from a 0.03° grid — crosses 시군구 borders (청계7 종로구 ↔ 청계8가 중구 is 72 m)."""
    buckets = defaultdict(list)
    cell = lambda x: (math.floor(x["lat"] / 0.03), math.floor(x["lng"] / 0.03))
    for x in lots:
        if x["lat"] is not None and x["lng"] is not None:
            buckets[cell(x)].append(x)

    def find(l):
        if l["lat"] is None or l["lng"] is None:
            return []
        cy, cx = cell(l)
        cand = []
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                for x in buckets.get((cy + dy, cx + dx), []):
                    if x is l:
                        continue
                    km = math.hypot((x["lat"] - l["lat"]) * 111, (x["lng"] - l["lng"]) * 88)
                    if km <= 2:
                        cand.append((km, x))
        return sorted(cand, key=lambda t: t[0])[:6]
    return find


def fee_line(l):
    if l["fee"] == "무료":
        return "무료"
    if not l["basic_won"] and not l["add_won"]:
        return "요금 미기재" if l["fee"] == "유료" else f"{l['fee']} (요금 미기재)"
    if l["basic_won"] is not None and l["basic_min"]:
        s = f"기본 {l['basic_min']}분 {won(l['basic_won'])}"
        if l["add_min"] and l["add_won"] is not None:
            s += f" · 추가 {l['add_min']}분당 {won(l['add_won'])}"
        return s
    if l["basic_won"] is not None and l["add_min"] and l["add_won"] is not None:
        return f"{l['add_min']}분당 {won(l['add_won'])}"
    return "요금 미기재" if l["fee"] == "유료" else f"{l['fee']} (요금 미기재)"


_EMD = re.compile(r"(?:^|\s|\()([가-힣0-9]+(?:읍|면|동|가))(?=$|\s|[,)])")


def emd_of(l):
    """읍면동: parcel address first ('서울특별시 양천구 신월동 915-4'), then the '(신월동' parenthetical of a road-name address,
    then the road address itself. Returns '' when none of them names an 읍/면/동 (roads like 테헤란로 are not neighbourhoods)."""
    for a in (l.get("addr_parcel") or "", l["addr"]):
        toks = a.split()
        m = _EMD.search(" ".join(toks[1:]) if toks else "")
        if m and not m.group(1).endswith(("시", "군", "구")):
            return m.group(1)
    return ""


def weekend_cells(l):
    """Saturday / holiday cell text for the 읍면동 table, same reading as lot.html: Seoul rows carry explicit 유무료,
    elsewhere 비고 text → '무료', 운영요일 without the day → '미운영'(no fee collection), otherwise '미확인'."""
    if l["fee"] == "무료":
        return {"sat": "무료", "hol": "무료"}

    def cell(free, unstaffed):
        if free:
            return "무료"
        if l.get("seoul_code") and free is False:
            return "유료"
        if unstaffed:
            return "미운영"
        return "미확인" if not l.get("seoul_code") else "유료"
    return {"sat": cell(l.get("sat_free"), l.get("sat_unstaffed")), "hol": cell(l.get("hol_free"), l.get("hol_unstaffed"))}


def hours_line(h):
    o, c = h
    if not o or not c or o == c:
        return ""  # 00:00~00:00 은 미기재로 본다
    return "24시간" if (o == "00:00" and c in ("23:59", "24:00")) else f"{o}~{c}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="/juchabi/")
    ap.add_argument("--origin", default="https://tbco-ship-it.github.io")
    ap.add_argument("--cname", default="")
    ap.add_argument("--adsense-pub", default="pub-8425563704095379")
    args = ap.parse_args()
    base = args.base if args.base.endswith("/") else args.base + "/"
    origin = args.origin.rstrip("/")
    today = dt.date.today()

    data = json.loads((ROOT / "data/lots.json").read_text())
    lots, source = data["lots"], data["source"]
    for l in lots:
        l["fee_line"] = fee_line(l)
        l["costs"] = {h: cost(l, h * 60) for h in HOURS}
        l["h_week"], l["h_sat"], l["h_hol"] = hours_line(l["hours"]["weekday"]), hours_line(l["hours"]["sat"]), hours_line(l["hours"]["holiday"])
        l["emd"] = emd_of(l)
        toks = l["addr"].split()
        l["locality"] = l["emd"] or next((t for t in toks[1:] if re.search(r"(읍|면|동|리|가|로|길)$", t) and not t.endswith(("시", "군", "구"))), "")
        l["wk"] = weekend_cells(l)
        l["disc_list"] = [(k, CAT_LABEL[k], v["pct"], v["text"]) for k in CAT_ORDER for v in [l["discounts"].get(k)] if v]
        ld = {"@context": "https://schema.org", "@type": "ParkingFacility", "name": l["name"], "address": {"@type": "PostalAddress", "streetAddress": l["addr"], "addressCountry": "KR"}}
        if l["lat"]:
            ld["geo"] = {"@type": "GeoCoordinates", "latitude": l["lat"], "longitude": l["lng"]}
        if l["phone"]:
            ld["telephone"] = l["phone"]
        if l["h_week"] and l["h_week"] != "24시간":
            ld["openingHours"] = "Mo-Fr " + l["h_week"].replace("~", "-")
        if l["fee"] in ("무료", "유료"):
            ld["isAccessibleForFree"] = l["fee"] == "무료"
        l["ld"] = ld
        l["latest"] = max(l["ref_date"], "")
    sidos = {}
    for l in lots:
        s = sidos.setdefault(l["sido_slug"], {"slug": l["sido_slug"], "name": l["sido"], "short": SIDO_SHORT[l["sido"]], "sigungu": {}, "lots": []})
        s["lots"].append(l)
        g = s["sigungu"].setdefault(l["sigungu"], {"name": l["sigungu"], "lots": []})
        g["lots"].append(l)

    def stats(lst):
        paid = [l for l in lst if l["fee"] != "무료"]
        free = [l for l in lst if l["fee"] == "무료"]
        h1 = sorted(c for c in (l["costs"][1] for l in paid) if c is not None)  # 첫 1시간 0원도 값이다
        return {"n": len(lst), "free": len(free), "paid": len(paid), "h1_med": median(h1) if h1 else None, "h1_n": len(h1),
                "h1_min": h1[0] if h1 else None, "h1_max": h1[-1] if h1 else None,
                "weekend_free": len([l for l in paid if l.get("sat_free") or l.get("hol_free")]),
                "monthly": len([l for l in lst if l["month_won"]]), "free_open": len([l for l in lst if l["free_open"]]),
                "disc": Counter(k for l in lst for k in l["discounts"]).most_common(4),
                "latest": max((l["ref_date"] for l in lst), default="")}
    for s in sidos.values():
        s["stats"] = stats(s["lots"])
        for g in s["sigungu"].values():
            g["stats"] = stats(g["lots"])
            g["lots"].sort(key=lambda l: (l["fee"] == "무료", l["name"]))  # 유료 먼저(사람들이 찾는 것), 이름순
            g["disc_summary"] = {k: Counter(l["discounts"][k]["pct"] for l in g["lots"] if k in l["discounts"] and l["discounts"][k]["pct"]).most_common(1) for k in CAT_ORDER}
            slugs = {l["slug"] for l in g["lots"]}
            emds = defaultdict(list)
            for l in g["lots"]:
                if l["emd"]:
                    emds[l["emd"]].append(l)
            g["emd"] = {}
            for name, lst in emds.items():
                if len(lst) < 2:
                    continue
                # 유료는 1시간 요금 싼 순(미기재 뒤), 그다음 무료 — 표에서 사람이 찾는 순서
                lst.sort(key=lambda l: (l["fee"] == "무료", l["costs"][1] is None, l["costs"][1] or 0, l["name"]))
                seg = name + ("-일대" if name in slugs else "")
                g["emd"][name] = {"name": name, "lots": lst, "stats": stats(lst), "path": f"{s['slug']}/{g['name']}/{seg}/"}
            g["emd"] = dict(sorted(g["emd"].items(), key=lambda kv: (-kv[1]["stats"]["n"], kv[0])))
            for l in g["lots"]:
                l["emd_path"] = g["emd"][l["emd"]]["path"] if l["emd"] in g["emd"] else ""
        s["sigungu"] = dict(sorted(s["sigungu"].items(), key=lambda kv: -kv[1]["stats"]["n"]))
    sidos = {k: sidos[k] for k in SIDO_ORDER if k in sidos}
    total = stats(lots)

    h = hashlib.md5()
    for f in sorted((ROOT / "static").glob("*")):
        h.update(f.read_bytes())
    h.update((ROOT / "data/lots.json").read_bytes())  # index.json changes with the data
    v = h.hexdigest()[:8]
    env = Environment(loader=FileSystemLoader(ROOT / "templates"), autoescape=select_autoescape(["html"]))
    env.filters["won"] = won
    env.globals.update(site=SITE, base=base, origin=origin, today=today.isoformat(), v=v, adsense_pub=args.adsense_pub,
                       sidos=sidos, total=total, HOURS=HOURS, CAT_LABEL=CAT_LABEL, CAT_ORDER=CAT_ORDER, source=source, n_lots=len(lots))

    if DIST.exists():
        shutil.rmtree(DIST)
    DIST.mkdir()
    shutil.copytree(ROOT / "static", DIST / "static")
    # search index (kept small: ~1.5 MB raw): [name, sido idx, 시군구, locality, slug (0 = same as name without spaces), lat, lng, fee, basic_min, basic_won, add_min, add_won, day_won, month_won, free_open?, spaces]
    sido_list = [[s["slug"], s["short"]] for s in sidos.values()]
    sido_idx = {s["slug"]: i for i, s in enumerate(sidos.values())}
    r5 = lambda x: round(x, 5) if x is not None else None
    items = [[l["name"], sido_idx[l["sido_slug"]], l["sigungu"], l["locality"], 0 if l["slug"] == re.sub(r"\s+", "", l["name"]) else l["slug"], r5(l["lat"]), r5(l["lng"]), {"무료": 0, "유료": 1}.get(l["fee"], 2),
              l["basic_min"], l["basic_won"], l["add_min"], l["add_won"], l["day_won"], l["month_won"], 1 if l["free_open"] else 0, l["spaces"],
              [l["costs"][hh] for hh in HOURS]] for l in lots]  # index 16: precomputed 1/2/3/5/8h — JS never recomputes
    stations = json.loads((ROOT / "data/stations.json").read_text())["stations"]  # [name, lat, lng] — "거제역" searches resolve to the station, then the nearest lots
    payload = json.dumps({"sidos": sido_list, "items": items, "stations": stations}, ensure_ascii=False, separators=(",", ":"))
    (DIST / "static/index.json").write_text(payload)
    env.globals["index_v"] = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]  # cache key from the index itself, not from CSS edits

    urls = []

    def write(path, template, **ctx):
        out = DIST / path
        out.mkdir(parents=True, exist_ok=True)
        (out / "index.html").write_text(env.get_template(template).render(path=path, **ctx))
        urls.append(path)

    write("", "index.html")
    for page in ("about", "methodology", "privacy", "contact"):
        write(f"{page}/", f"{page}.html")
    write("guide/discount/", "guide_discount.html")
    write("guide/fee/", "guide_fee.html")
    cheap_month = sorted([l for l in lots if l["month_won"] and l["month_won"] >= 10000], key=lambda l: l["month_won"])
    write("guide/monthly/", "guide_monthly.html", cheap=cheap_month[:60], by_sido={s["slug"]: sorted([l for l in s["lots"] if l["month_won"] and l["month_won"] >= 10000], key=lambda l: l["month_won"])[:10] for s in sidos.values()})
    write("guide/free-open/", "guide_free.html", free_open=[l for l in lots if l["free_open"]])
    find_nearby = make_nearby(lots)
    write("regions/", "regions.html")
    for s in sidos.values():
        write(f"{s['slug']}/", "sido.html", s=s)
        for g in s["sigungu"].values():
            write(f"{s['slug']}/{g['name']}/", "sigungu.html", s=s, g=g)
            for e in g["emd"].values():
                e["others"] = [o for o in g["emd"].values() if o is not e][:24]
                write(e["path"], "emd.html", s=s, g=g, e=e)
            for l in g["lots"]:
                write(l["path"], "lot.html", s=s, g=g, l=l, near=find_nearby(l))

    # sitemap (split at 40k urls to stay well under the 50k/50MB limit)
    chunks = [urls[i:i + 40000] for i in range(0, len(urls), 40000)]
    names = []
    for i, ch in enumerate(chunks):
        name = "sitemap.xml" if len(chunks) == 1 else f"sitemap-{i + 1}.xml"
        names.append(name)
        sm = ['<?xml version="1.0" encoding="UTF-8"?>', '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
        for u in ch:
            sm.append(f"<url><loc>{escape(origin + base + u)}</loc></url>")  # no lastmod: we don't track per-page change dates
        sm.append("</urlset>")
        (DIST / name).write_text("\n".join(sm))
    if len(chunks) > 1:
        (DIST / "sitemap.xml").write_text('<?xml version="1.0" encoding="UTF-8"?>\n<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + "".join(f"<sitemap><loc>{origin}{base}{n}</loc></sitemap>" for n in names) + "</sitemapindex>")
    (DIST / "robots.txt").write_text(f"User-agent: *\nAllow: /\nSitemap: {origin}{base}sitemap.xml\n")
    (DIST / "404.html").write_text(env.get_template("404.html").render(path="404"))
    (DIST / ".nojekyll").write_text("")
    key = (ROOT / "static/indexnow-key.txt").read_text().strip()
    (DIST / f"{key}.txt").write_text(key + "\n")
    if args.adsense_pub:
        (DIST / "ads.txt").write_text(f"google.com, {args.adsense_pub}, DIRECT, f08c47fec0942fa0\n")
    if args.cname:
        (DIST / "CNAME").write_text(args.cname + "\n")
    print(f"built {len(urls)} pages ({len(lots)} lots, {sum(len(s['sigungu']) for s in sidos.values())} 시군구, {sum(len(g['emd']) for s in sidos.values() for g in s['sigungu'].values())} 읍면동) -> {DIST}")


if __name__ == "__main__":
    main()
