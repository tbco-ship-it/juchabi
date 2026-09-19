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
    return f"{n:,}원" if isinstance(n, int) else "—"


def cost(l, minutes):
    """Fee for `minutes` under the reported 기본시간/기본요금/추가단위 rule; None when the lot has no usable rule."""
    if l["fee"] == "무료":
        return 0
    if l.get("fee_review"):
        return None  # 복합·소수 요금은 원문 보존, 계산 보류
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
    if l["day_won"] and (not l["day_hours"] or l["day_hours"] >= 24 or minutes <= l["day_hours"] * 60):
        total = min(total, l["day_won"])
    return total


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
        toks = l["addr"].split()
        l["locality"] = next((t for t in toks[1:] if re.search(r"(읍|면|동|리|가|로|길)$", t) and not t.endswith(("시", "군", "구"))), "")
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
        return {"n": len(lst), "free": len(free), "paid": len(paid), "h1_med": h1[len(h1) // 2] if h1 else None,
                "monthly": len([l for l in lst if l["month_won"]]), "free_open": len([l for l in lst if l["free_open"]]),
                "disc": Counter(k for l in lst for k in l["discounts"]).most_common(4),
                "latest": max((l["ref_date"] for l in lst), default="")}
    for s in sidos.values():
        s["stats"] = stats(s["lots"])
        for g in s["sigungu"].values():
            g["stats"] = stats(g["lots"])
            g["lots"].sort(key=lambda l: (l["fee"] == "무료", l["name"]))  # 유료 먼저(사람들이 찾는 것), 이름순
            g["disc_summary"] = {k: Counter(l["discounts"][k]["pct"] for l in g["lots"] if k in l["discounts"] and l["discounts"][k]["pct"]).most_common(1) for k in CAT_ORDER}
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
    (DIST / "static/index.json").write_text(json.dumps({"sidos": sido_list, "items": items}, ensure_ascii=False, separators=(",", ":")))

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
    write("regions/", "regions.html")
    for s in sidos.values():
        write(f"{s['slug']}/", "sido.html", s=s)
        for g in s["sigungu"].values():
            write(f"{s['slug']}/{g['name']}/", "sigungu.html", s=s, g=g)
            for l in g["lots"]:
                near = []
                if l["lat"]:
                    near = sorted([(math.hypot((x["lat"] - l["lat"]) * 111, (x["lng"] - l["lng"]) * 88), x) for x in g["lots"] if x is not l and x["lat"]], key=lambda t: t[0])[:6]
                write(l["path"], "lot.html", s=s, g=g, l=l, near=near)

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
    print(f"built {len(urls)} pages ({len(lots)} lots, {sum(len(s['sigungu']) for s in sidos.values())} 시군구) -> {DIST}")


if __name__ == "__main__":
    main()
