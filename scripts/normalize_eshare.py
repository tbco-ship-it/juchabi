#!/usr/bin/env python3
"""Analyze and optionally apply ShareNuri open-parking records.

The normalizer is intentionally offline-first.  It reads the combined artifact
provided by the owner, never calls the API, and only writes product data when
an already-reviewed candidate is passed with its explicit digest.
"""
from __future__ import annotations

import argparse
from decimal import Decimal, InvalidOperation
import hashlib
import html
import json
import math
import os
import posixpath
import random
import re
import tempfile
import unicodedata
from collections import Counter, defaultdict
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

import fcntl


ROOT = Path(__file__).resolve().parent.parent
WORKSPACE = next((p for p in (ROOT, *ROOT.parents) if (p / "OUTBOX").is_dir()), ROOT)
DEFAULT_INPUT = WORKSPACE / "OUTBOX" / "JUCHABI_ESHARE_PARKING_RAW_20260921.json"
DEFAULT_CANDIDATE = ROOT / "data/wip/eshare_candidate.json"
DEFAULT_REVIEW = ROOT / "data/wip/eshare_review.json"

SIDO = {
    "서울특별시": ("seoul", "서울"),
    "부산광역시": ("busan", "부산"),
    "대구광역시": ("daegu", "대구"),
    "인천광역시": ("incheon", "인천"),
    "전남광주통합특별시": ("gwangju-jeonnam", "전남·광주"),
    "대전광역시": ("daejeon", "대전"),
    "울산광역시": ("ulsan", "울산"),
    "세종특별자치시": ("sejong", "세종"),
    "경기도": ("gyeonggi", "경기"),
    "강원특별자치도": ("gangwon", "강원"),
    "충청북도": ("chungbuk", "충북"),
    "충청남도": ("chungnam", "충남"),
    "전북특별자치도": ("jeonbuk", "전북"),
    "경상북도": ("gyeongbuk", "경북"),
    "경상남도": ("gyeongnam", "경남"),
    "제주특별자치도": ("jeju", "제주"),
}
SIDO_ALIASES = {
    "서울": "서울특별시", "서울특별시": "서울특별시",
    "부산": "부산광역시", "부산광역시": "부산광역시",
    "대구": "대구광역시", "대구광역시": "대구광역시",
    "인천": "인천광역시", "인천광역시": "인천광역시",
    "광주": "전남광주통합특별시", "광주광역시": "전남광주통합특별시",
    "전남": "전남광주통합특별시", "전라남도": "전남광주통합특별시",
    "전남광주통합특별시": "전남광주통합특별시",
    "대전": "대전광역시", "대전광역시": "대전광역시",
    "울산": "울산광역시", "울산광역시": "울산광역시",
    "세종": "세종특별자치시", "세종시": "세종특별자치시", "세종특별자치시": "세종특별자치시",
    "경기": "경기도", "경기도": "경기도",
    "강원": "강원특별자치도", "강원도": "강원특별자치도", "강원특별자치도": "강원특별자치도",
    "충북": "충청북도", "충청북도": "충청북도",
    "충남": "충청남도", "충청남도": "충청남도",
    "전북": "전북특별자치도", "전라북도": "전북특별자치도", "전북특별자치도": "전북특별자치도",
    "경북": "경상북도", "경상북도": "경상북도",
    "경남": "경상남도", "경상남도": "경상남도",
    "제주": "제주특별자치도", "제주도": "제주특별자치도", "제주특별자치도": "제주특별자치도",
}
# A sigungu token is later used in an output path.  Keep the grammar narrow so
# an API address can never smuggle a path separator or dot-segment into a lot
# URL.  The provider uses Korean administrative names, including a small
# number of numeric/dot-separated names such as "세종시" and "광주 북구".
SIGOONGU_PREFIX = re.compile(r"^[가-힣0-9·-]+(?:시|군|구)$")
CENTERS = {
    "seoul": (37.5665, 126.9780), "gyeonggi": (37.4138, 127.5183),
    "incheon": (37.4563, 126.7052), "busan": (35.1796, 129.0756),
    "daegu": (35.8714, 128.6014), "daejeon": (36.3504, 127.3845),
    "gwangju-jeonnam": (35.1595, 126.8526), "ulsan": (35.5384, 129.3114),
    "sejong": (36.4800, 127.2890), "gangwon": (37.8228, 128.1555),
    "chungbuk": (36.6357, 127.4910), "chungnam": (36.5184, 126.8000),
    "jeonbuk": (35.8203, 127.1088), "gyeongbuk": (36.5760, 128.5056),
    "gyeongnam": (35.4606, 128.2132), "jeju": (33.4996, 126.5312),
}
BLOCK_TAGS = {"address", "article", "br", "div", "li", "p", "section", "tr"}
DROP_TAGS = {"script", "style", "svg"}
NAME_NOISE = re.compile(r"공영주차장|공영|주차장|주차동|노외|노상|부설|무료|유료")
TRAILING_ADDRESS = re.compile(r"(?:\s*(?:일대|일원|번지|주변|부근)|\s*외\s*\d+\s*필지)+\s*$")
ROAD_END = re.compile(r"(?:대로|로|길|거리)\s*(?:지하\s*)?\d+(?:-\d+)?\s*$")
PARCEL_END = re.compile(r"(?:동|리|가)\s*(?:산\s*)?\d+(?:-\d+)?(?:번지)?\s*$")
ESHARE_HOSTS = {"www.eshare.go.kr", "eshare.go.kr"}
OPEN_LABELS = ("평일개방", "토요일개방", "휴일개방")


def slugify_ko(name: str) -> str:
    s = re.sub(r"\s+", "", name or "")
    s = re.sub(r"[()\[\]{}<>,./\\?!@#$%^&*'\"`:;|~=+·ㆍ]", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s or "주차장"


def normalize_name(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).lower()
    text = NAME_NOISE.sub("", text)
    return re.sub(r"[^0-9a-z가-힣]", "", text)


def source_name_text(row: dict[str, Any]) -> str:
    """Remove the provider's appended address tail from a resource name.

    A subset of list records puts ``name + address`` in ``rsrcNm`` while the
    same API exposes the address separately.  Keeping that tail in the name
    key makes its parcel numbers look like facility-number conflicts (for
    example 제1 + 1574-42) and can reject an otherwise certain match.
    """
    text = str(row.get("rsrcNm") or "").strip()
    if not text:
        return str(row.get("lcInf") or "").strip()
    best_start: int | None = None
    for value in (row.get("addr"), row.get("lcInf")):
        raw = str(value or "").strip()
        tokens = raw.split()
        starts = [index for index, token in enumerate(tokens) if re.search(r"(?:시|군|구)$", token)]
        for start in starts:
            # Match progressively shorter address prefixes so a provider's
            # extra suffix (번지, 이동민원실 옆, etc.) cannot defeat the cut.
            for end in range(len(tokens), start + 1, -1):
                if end - start < 2:
                    continue
                pattern = r"\s*".join(re.escape(token) for token in tokens[start:end])
                match = re.search(pattern, text)
                if match and match.start() > 0 and (best_start is None or match.start() < best_start):
                    best_start = match.start()
    if best_start is not None:
        return text[:best_start].rstrip(" ,-/")
    # ``daddr`` is a provider display/location hint, not a replacement for
    # the primary facility name.  Falling back to it can erase facility
    # numbers (e.g. 제12 vs 제1) and let a conflicting weak-name match pass.
    return text


def _address_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = re.sub(r"\([^)]*\)", "", text)
    text = TRAILING_ADDRESS.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def address_system(value: Any) -> str | None:
    """Classify only addresses with an unambiguous final road/parcel number."""
    text = _address_text(value)
    if not text:
        return None
    if ROAD_END.search(text):
        return "road"
    if PARCEL_END.search(text):
        return "parcel"
    return None


def address_key(value: Any) -> tuple[str, str] | None:
    text = _address_text(value)
    system = address_system(text)
    if not system:
        return None
    return system, re.sub(r"[\s,]+", "", text)


def distance_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    radius = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return radius * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def parse_coordinate(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def classify_sido(address: Any) -> tuple[str | None, str | None, str | None]:
    tokens = re.sub(r"\s+", " ", str(address or "")).strip().split(" ")
    raw_prefix = tokens[0] if tokens else ""
    sido = SIDO_ALIASES.get(raw_prefix)
    if not sido:
        # A small number of provider strings omit spaces after the province;
        # accept only an explicit known prefix, never a fuzzy province guess.
        for prefix in sorted(SIDO_ALIASES, key=len, reverse=True):
            if raw_prefix.startswith(prefix) and len(raw_prefix) > len(prefix):
                sido = SIDO_ALIASES[prefix]
                tokens = [prefix, raw_prefix[len(prefix):], *tokens[1:]]
                break
    if not sido:
        return None, None, "unknown_sido_prefix"
    slug = SIDO[sido][0]
    if sido == "세종특별자치시":
        return sido, slug, "세종시"
    second = tokens[1] if len(tokens) > 1 else ""
    match = SIGOONGU_PREFIX.fullmatch(second)
    if not match:
        return sido, slug, "bad_sigungu"
    sigungu = match.group(0)
    if sido == "전남광주통합특별시" and raw_prefix in {"광주", "광주광역시"}:
        sigungu = f"광주 {sigungu}"
    return sido, slug, sigungu


def geography_status(row: dict[str, Any]) -> dict[str, Any]:
    sido, sido_slug, sigungu = classify_sido(row.get("addr"))
    lat, lng = parse_coordinate(row.get("lat")), parse_coordinate(row.get("lot"))
    if not sido:
        return {"ok": False, "reason": "unknown_sido_prefix", "sido": None, "sido_slug": None, "sigungu": None}
    if (not sigungu or not isinstance(sigungu, str) or sigungu == "bad_sigungu"
            or not SIGOONGU_PREFIX.fullmatch(sigungu.replace(" ", "", 1) if sido == "전남광주통합특별시" else sigungu)):
        return {"ok": False, "reason": "bad_sigungu", "sido": sido, "sido_slug": sido_slug, "sigungu": sigungu}
    if lat is None or lng is None or not (33 <= lat <= 39 and 124 <= lng <= 132):
        return {"ok": False, "reason": "coordinate_outside_korea_bbox", "sido": sido, "sido_slug": sido_slug, "sigungu": sigungu, "lat": lat, "lng": lng}
    center = CENTERS[sido_slug]
    center_distance = distance_m(lat, lng, *center)
    if center_distance > 200_000:
        return {"ok": False, "reason": "coordinate_outside_sido_radius", "sido": sido, "sido_slug": sido_slug, "sigungu": sigungu, "lat": lat, "lng": lng, "center_distance_m": round(center_distance, 1)}
    return {"ok": True, "sido": sido, "sido_slug": sido_slug, "sigungu": sigungu, "lat": lat, "lng": lng, "center_distance_m": round(center_distance, 1)}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in DROP_TAGS:
            self.skip_depth += 1
        elif not self.skip_depth and tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in DROP_TAGS and self.skip_depth:
            self.skip_depth -= 1
        elif not self.skip_depth and tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skip_depth:
            self.parts.append(data)


def html_to_text(*values: Any, limit: int | None = 600) -> str:
    parser = _TextExtractor()
    for value in values:
        if value:
            parser.feed(str(value))
            parser.parts.append("\n")
    parser.close()
    raw = html.unescape("".join(parser.parts)).replace("\r", "")
    lines = [re.sub(r"[ \t\f\v]+", " ", line).strip() for line in raw.split("\n")]
    text = "\n".join(line for line in lines if line)
    return text if limit is None else text[:limit].rstrip()


def safe_eshare_url(value: Any) -> str:
    url = str(value or "").strip()
    # Backslashes are interpreted as separators by some browsers even though
    # urlparse treats them as ordinary netloc characters.  Credentials and
    # explicit ports likewise make the displayed host ambiguous.
    if not url or "\\" in url or any(ord(char) < 0x20 for char in url):
        return ""
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return ""
    if (parsed.scheme.lower() != "https" or hostname not in ESHARE_HOSTS or port is not None
            or parsed.username is not None or parsed.password is not None
            or not parsed.path.startswith("/")):
        return ""
    return url


def open_days_from_values(*values: Any) -> str:
    """Preserve the three institution availability tokens without parsing hours."""
    raw = html.unescape(" ".join(str(value or "") for value in values))
    # Accept the provider's literal ``<label>`` tokens and the plain
    # ``label: value`` variant.  Do not infer a triple from label text split
    # across arbitrary rich-text spans; that was observed to swallow an
    # unrelated note into the holiday value.
    label_forms = {
        label: re.compile(rf"(?:<\s*{label}\s*>|(?<![가-힣]){label}\s*[:：])", re.I)
        for label in OPEN_LABELS
    }
    if not all(pattern.search(raw) for pattern in label_forms.values()):
        return ""
    text = raw
    for label, pattern in label_forms.items():
        text = pattern.sub(label + " ", text)
    text = re.sub(r"</?(?:address|article|br|div|li|p|section|tr)\b[^>]*>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r"\n[ \t]*", "\n", text).strip()
    found: dict[str, str] = {}
    pattern = re.compile(r"(평일개방|토요일개방|휴일개방)\s*(?:[:：\)]\s*)?", re.I)
    matches = list(pattern.finditer(text))
    for index, match in enumerate(matches):
        label = match.group(1)
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        value = re.sub(r"[ \t]+", " ", text[match.end():end]).split("\n", 1)[0].strip(" \t:：)-").strip()
        if value and label not in found:
            found[label] = value
    if not all(label in found for label in OPEN_LABELS):
        return ""
    return f"평일 {found['평일개방']} · 토 {found['토요일개방']} · 휴일 {found['휴일개방']}"


def fee_evidence(note: str, free_code: str) -> tuple[str, bool, dict[str, str]]:
    """Classify free-window notes without parsing their numeric fee schedule."""
    lines = [line.strip() for line in note.splitlines() if line.strip()]
    fee_lines: list[str] = []
    fee_label = re.compile(r"(?:사용|이용|주차|충전)?요금(?:정보)?|(?:사용|이용)료(?!\s*(?:납부|환불|안내|조건|취소))")
    free_value = re.compile(r"^(?:무료|무료이용|무료운영|없음|무|해당없음|해당 사항 없음|해당사항 없음|해당사항없음|없습니다?)(?:\s*[.。,/)]|\s*$)")
    amount_pattern = re.compile(r"(?<!\d)(\d[\d,]*(?:\.\d+)?)\s*원")

    def explicit_charge(value: str) -> bool:
        """Return whether a fee-label value explicitly says charging applies.

        A zero-priced allowance and a later ``요금 부과`` clause may share one
        line.  The zero-amount exception must not hide that later clause, but
        Negative forms are excluded from this specific signal; the existing
        conservative fee-label fallback remains unchanged for other text.
        """
        for match in re.finditer(r"유료|부과|징수|청구|납부|발생|적용", value):
            suffix = value[match.end():]
            prefix = value[:match.start()]
            if re.match(r"\s*(?:없음|없습니다?|안함|하지\s*않|되지\s*않|아님|아니)", suffix):
                continue
            if re.search(r"(?:미|안)\s*$", prefix):
                continue
            return True
        return False

    for line in lines:
        amounts: list[Decimal] = []
        for amount in amount_pattern.findall(line):
            try:
                amounts.append(Decimal(amount.replace(",", "")))
            except InvalidOperation:
                continue
        has_positive_amount = any(amount > 0 for amount in amounts)
        if re.search(r"유료", line):
            fee_lines.append(line)
        # 0원 is a free amount, not evidence that a free-window resource is
        # paid.  A line containing both 0원 and a positive amount is paid.
        if has_positive_amount:
            fee_lines.append(line)
        for match in fee_label.finditer(line):
            value = line[match.end():].lstrip(" \t:：>)-=")
            value = value.split("<", 1)[0].strip(" \t:：>)-=/.,。")
            if explicit_charge(value):
                fee_lines.append(line)
            elif (value and "무료" not in value and not free_value.match(value)
                  and not (amounts and not has_positive_amount)):
                fee_lines.append(line)
    if free_code == "N":
        return "유료", True, {"공유누리": (fee_lines[0] if fee_lines else "freeYn=N")}
    unique = list(dict.fromkeys(fee_lines))
    if unique:
        return "혼합", True, {"공유누리": " / ".join(unique)}
    return "무료", False, {}


def numeric_tokens(value: Any) -> set[str]:
    return {str(int(token)) for token in re.findall(r"\d+", str(value or ""))}


def numeric_name_conflict(left: Any, right: Any) -> bool:
    left_tokens, right_tokens = numeric_tokens(left), numeric_tokens(right)
    # Containment is unsafe when both names carry numbers but those numbers
    # are not the same.  Disjointness alone missed 101동 제1 vs 101동 제12
    # because both shared the 101 token.
    return bool(left_tokens and right_tokens and left_tokens != right_tokens)


def load_raw(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    data = json.loads(path.read_text())
    list_rows = data.get("list")
    detail_rows = data.get("detail")
    if not isinstance(list_rows, list) or not isinstance(detail_rows, list):
        raise ValueError("raw artifact must contain list and detail arrays")
    list_ids = [str(r.get("rsrcNo") or "") for r in list_rows]
    detail_ids = [str(r.get("rsrcNo") or "") for r in detail_rows]
    if not all(list_ids) or not all(detail_ids):
        raise ValueError("raw artifact contains a row without rsrcNo")
    if len(set(list_ids)) != len(list_ids) or len(set(detail_ids)) != len(detail_ids):
        raise ValueError("raw artifact contains duplicate rsrcNo values")
    if set(list_ids) != set(detail_ids):
        missing_detail = sorted(set(list_ids) - set(detail_ids))[:5]
        missing_list = sorted(set(detail_ids) - set(list_ids))[:5]
        raise ValueError(f"raw list/detail rsrcNo sets differ: missing_detail={missing_detail}, missing_list={missing_list}")
    by_list = {str(r["rsrcNo"]): r for r in list_rows}
    merged: list[dict[str, Any]] = []
    for detail in detail_rows:
        row = dict(by_list.get(str(detail["rsrcNo"]), {}))
        for key, value in detail.items():
            if value not in (None, "") or key not in row:
                row[key] = value
        merged.append(row)
    return merged, detail_rows


def lot_key(lot: dict[str, Any]) -> str:
    return "|".join(str(lot.get(k) or "") for k in ("id", "name", "addr_parcel"))


def lot_ref(index: int, lot: dict[str, Any]) -> dict[str, Any]:
    return {"lot_index": index, "lot_id": lot.get("id", ""), "lot_key": lot_key(lot),
            "name": lot.get("name", ""), "addr": lot.get("addr", ""), "addr_parcel": lot.get("addr_parcel", ""),
            "sido": lot.get("sido", ""), "sigungu": lot.get("sigungu", "")}


def _candidate_refs(indices: Iterable[int], lots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [lot_ref(i, lots[i]) for i in sorted(set(indices))]


def build_indexes(lots: list[dict[str, Any]]) -> tuple[dict[str, set[int]], dict[tuple[str, str], set[int]], list[str]]:
    names: dict[str, set[int]] = defaultdict(set)
    addresses: dict[tuple[str, str], set[int]] = defaultdict(set)
    name_keys = [normalize_name(lot.get("name")) for lot in lots]
    for i, lot in enumerate(lots):
        key = name_keys[i]
        if key:
            names[key].add(i)
        for value in (lot.get("addr"), lot.get("addr_parcel")):
            key = address_key(value)
            if key:
                addresses[key].add(i)
    return names, addresses, name_keys


def _resource_meta(row: dict[str, Any]) -> dict[str, Any]:
    return {"rsrcNo": row.get("rsrcNo", ""), "name": row.get("rsrcNm", ""), "addr": row.get("addr", ""),
            "lat": row.get("lat"), "lng": row.get("lot"), "inst": row.get("rsrcInstNm", ""),
            "type": row.get("rsrcClsNm", ""), "freeYn": row.get("freeYn", ""), "updYmd": row.get("updYmd", ""),
            "url": safe_eshare_url(row.get("instUrlAddr"))}


def _eshare_value(row: dict[str, Any]) -> dict[str, Any]:
    return {"rsrcNo": str(row.get("rsrcNo") or ""), "url": safe_eshare_url(row.get("instUrlAddr")), "free": str(row.get("freeYn") or "").upper(),
            "inst": row.get("rsrcInstNm") or "", "upd": row.get("updYmd") or ""}


def _new_lot(row: dict[str, Any], geo: dict[str, Any]) -> dict[str, Any]:
    type_name = (row.get("rsrcClsNm") or "노외").strip()
    if type_name == "주차장":
        type_name = "노외"
    name = (row.get("rsrcNm") or row.get("lcInf") or f"공유누리 {row.get('rsrcNo')}").strip()
    # Keep the full cleaned text for classification.  Only the stored display
    # note is truncated; otherwise a paid clause after byte/character 600 can
    # incorrectly turn a mixed resource into a free one.
    full_note = html_to_text(row.get("rsrcIntr"), row.get("atpn"), limit=None)
    note = full_note[:600].rstrip()
    free_code = str(row.get("freeYn") or "").upper()
    fee, fee_review, fee_raw = fee_evidence(full_note, free_code)
    open_days = open_days_from_values(row.get("rsrcIntr"), row.get("atpn"))
    return {
        "id": f"eshare-{row['rsrcNo']}", "name": name, "kind": "개방", "type": type_name,
        "addr": re.sub(r"\s+", " ", str(row.get("addr") or "")).strip(), "addr_parcel": "",
        "sido": geo["sido"], "sido_slug": geo["sido_slug"], "sigungu": geo["sigungu"], "gu": "",
        "spaces": None, "grade": "", "rotation": "", "oper_day": "", "hours": None,
        "fee": fee, "basic_min": None, "basic_won": None,
        "add_min": None, "add_won": None, "day_hours": None, "day_won": None, "month_won": None,
        "hourly_review": False, "daily_review": False, "monthly_review": False, "fee_review": fee_review, "fee_raw": fee_raw,
        "pay": "", "note": "", "eshare_note": note, "discounts": {},
        "free_open": "공유누리 개방 무료 · 개방 시간은 기관 안내 참조" if free_code == "Y" and fee == "무료" else "",
        "open_days": open_days,
        "org": row.get("rsrcInstNm") or "", "phone": "", "lat": geo["lat"], "lng": geo["lng"],
        "disabled_zone": False, "ref_date": row.get("updYmd") or "", "provider": "공유누리",
        "eshare": _eshare_value(row), "eshare_rsrc_cls_cd": row.get("rsrcClsCd") or "",
    }


def analyze_rows(rows: list[dict[str, Any]], lots: list[dict[str, Any]]) -> dict[str, Any]:
    names, addresses, lot_name_keys = build_indexes(lots)
    existing_sources: dict[str, list[int]] = defaultdict(list)
    for index, lot in enumerate(lots):
        source = lot.get("eshare")
        if isinstance(source, dict) and source.get("rsrcNo"):
            existing_sources[str(source["rsrcNo"])].append(index)
    coord_rows: list[tuple[int, float, float]] = []
    for i, lot in enumerate(lots):
        lat, lng = parse_coordinate(lot.get("lat")), parse_coordinate(lot.get("lng"))
        if lat is not None and lng is not None:
            coord_rows.append((i, lat, lng))
    matched: list[dict[str, Any]] = []
    new_lots: list[dict[str, Any]] = []
    reviews: list[dict[str, Any]] = []
    used_destinations: dict[int, str] = {}
    seen_source_ids: set[str] = set()
    for row in rows:
        meta = _resource_meta(row)
        source_id = str(row.get("rsrcNo") or "")
        if not source_id:
            reviews.append({"source": meta, "reason": "missing_rsrc_no", "candidates": {}})
            continue
        if source_id in seen_source_ids:
            reviews.append({"source": meta, "reason": "duplicate_input_rsrc_no", "candidates": {}})
            continue
        seen_source_ids.add(source_id)
        if source_id in existing_sources:
            reviews.append({"source": meta, "reason": "source_already_present", "candidates": {
                "existing": _candidate_refs(existing_sources[source_id], lots),
            }})
            continue
        free_code = str(row.get("freeYn") or "").upper()
        if free_code not in {"Y", "N"}:
            reviews.append({"source": meta, "reason": "unknown_free_yn", "candidates": {}})
            continue
        if not safe_eshare_url(row.get("instUrlAddr")):
            reviews.append({"source": meta, "reason": "invalid_source_url", "candidates": {}})
            continue
        geo = geography_status(row)
        if not geo["ok"]:
            reviews.append({"source": meta, "reason": geo["reason"], "geography": geo, "candidates": {}})
            continue
        source_name = source_name_text(row)
        source_name_key = normalize_name(row.get("rsrcNm"))
        name_hits = names.get(source_name_key, set()) if source_name_key else set()
        name_weak_hits = {i for i, lot_name_key in enumerate(lot_name_keys)
                          if i not in name_hits and len(source_name_key) >= 2 and len(lot_name_key) >= 2
                          and (source_name_key in lot_name_key or lot_name_key in source_name_key)
                          and not numeric_name_conflict(source_name, lots[i].get("name"))}
        source_lat, source_lng = geo["lat"], geo["lng"]
        coord_hits = {i for i, lat, lng in coord_rows if distance_m(source_lat, source_lng, lat, lng) <= 50}
        source_address = address_key(row.get("addr"))
        address_hits = addresses.get(source_address, set()) if source_address else set()
        intersections = {
            "name_coord": name_hits & coord_hits,
            "name_address": name_hits & address_hits,
            "name_weak_coord": name_weak_hits & coord_hits if len(coord_hits) == 1 else set(),
        }
        candidates = {"name": _candidate_refs(name_hits, lots), "coord_50m": _candidate_refs(coord_hits, lots),
                      "name_weak": _candidate_refs(name_weak_hits, lots),
                      "address": _candidate_refs(address_hits, lots),
                      "name_coord": _candidate_refs(intersections["name_coord"], lots),
                      "name_address": _candidate_refs(intersections["name_address"], lots),
                      "name_weak_coord": _candidate_refs(intersections["name_weak_coord"], lots)}
        strong_decisive = intersections["name_coord"] | intersections["name_address"]
        weak_decisive = intersections["name_weak_coord"]
        decisive = strong_decisive | weak_decisive
        if len(decisive) == 1:
            destination = next(iter(decisive))
            available_conflict = any(evidence and destination not in evidence for evidence in (coord_hits, address_hits))
            if available_conflict:
                reviews.append({"source": meta, "reason": "available_evidence_conflict", "geography": geo, "candidates": candidates})
            elif len(strong_decisive) > 1 or len(weak_decisive) > 1:
                reviews.append({"source": meta, "reason": "multiple_decisive_destinations", "geography": geo, "candidates": candidates})
            elif destination in used_destinations or lots[destination].get("eshare"):
                reviews.append({"source": meta, "reason": "destination_already_assigned", "geography": geo, "candidates": candidates,
                                 "existing_source": used_destinations.get(destination) or lots[destination].get("eshare")})
            elif lots[destination].get("eshare_note"):
                reviews.append({"source": meta, "reason": "destination_already_has_source_note", "geography": geo, "candidates": candidates})
            elif lots[destination].get("open_days"):
                reviews.append({"source": meta, "reason": "destination_already_has_open_days", "geography": geo, "candidates": candidates})
            else:
                used_destinations[destination] = str(row.get("rsrcNo") or "")
                how = "name+coord" if destination in intersections["name_coord"] else ("name+address" if destination in intersections["name_address"] else "name-contains+coord")
                matched.append({"source": meta, "lot": lot_ref(destination, lots[destination]), "match": how, "eshare": _eshare_value(row),
                                "eshare_note": html_to_text(row.get("rsrcIntr"), row.get("atpn"), limit=600),
                                "free_open": "공유누리 개방 무료 · 개방 시간은 기관 안내 참조"
                                if free_code == "Y" and lots[destination].get("fee") == "무료" else "",
                                "open_days": open_days_from_values(row.get("rsrcIntr"), row.get("atpn"))})
            continue
        # A containment-only name is confirmation, not a standalone identity
        # signal.  It may strengthen a unique coordinate candidate above, but
        # must not turn an otherwise unmatched source into a review-only row.
        evidence_count = sum(bool(x) for x in (name_hits, coord_hits, address_hits))
        if evidence_count:
            reviews.append({"source": meta, "reason": "insufficient_or_conflicting_evidence", "geography": geo, "candidates": candidates,
                             "evidence": {"name_count": len(name_hits), "coord_count": len(coord_hits), "address_count": len(address_hits)}})
            continue
        new_lots.append(_new_lot(row, geo))
    return {"matched": matched, "new_lots": new_lots, "reviews": reviews, "used_destinations": used_destinations}


def assign_new_paths(lots: list[dict[str, Any]], new_lots: list[dict[str, Any]]) -> None:
    # Reserve the final slug, not only a base-name counter.  Existing data may
    # already contain a literal ``foo-2`` slug, so incrementing the base count
    # alone can still collide with a real slug.
    used: dict[tuple[str, str], set[str]] = defaultdict(set)
    for lot in lots:
        group = (lot.get("sido_slug", ""), lot.get("sigungu", ""))
        used[group].add(lot.get("slug") or slugify_ko(lot.get("name", "")))
    for lot in sorted(new_lots, key=lambda x: str(x.get("id", ""))):
        group = (lot["sido_slug"], lot["sigungu"])
        slugs = used[group]
        base = slugify_ko(lot["name"])
        slug = base
        suffix = 2
        while slug in slugs:
            slug = f"{base}-{suffix}"
            suffix += 1
        slugs.add(slug)
        lot["slug"] = slug
        lot["path"] = f"{lot['sido_slug']}/{lot['sigungu']}/{lot['slug']}/"


def _validate_relative_output_path(value: Any) -> str:
    path = str(value or "")
    raw_parts = path.split("/")
    interior_parts = raw_parts[:-1] if path.endswith("/") else raw_parts
    parts = Path(path).parts
    if (not path or not path.endswith("/") or path.startswith("/") or "\\" in path
            or "\x00" in path or any(part in {"", ".", ".."} for part in interior_parts)
            or any(part in {".", ".."} for part in parts)
            or Path(path).is_absolute()):
        raise RuntimeError(f"unsafe relative output path: {value!r}")
    return path


def canonical_output_path(value: Any) -> str:
    """Return the physical route identity after strict path validation."""
    path = _validate_relative_output_path(value)
    return posixpath.normpath(path).rstrip("/") + "/"


def _source_digest(lots: list[dict[str, Any]]) -> str:
    raw = json.dumps(lots, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_candidate(data: dict[str, Any], rows: list[dict[str, Any]], analysis: dict[str, Any], raw_path: Path) -> dict[str, Any]:
    lots = data["lots"]
    new_lots = analysis["new_lots"]
    assign_new_paths(lots, new_lots)
    source_counter = Counter()
    for row in rows:
        sido, _, _ = classify_sido(row.get("addr"))
        source_counter[sido or "검토"] += 1
    accepted_counter = Counter(lot["sido"] for lot in new_lots)
    matched_counter = Counter()
    for item in analysis["matched"]:
        matched_counter[item["lot"]["sido"]] += 1
    free_total = sum(str(row.get("freeYn") or "").upper() == "Y" for row in rows)
    free_new = sum(lot["fee"] == "무료" for lot in new_lots)
    free_matched = sum(item["eshare"]["free"] == "Y" for item in analysis["matched"])
    fee_new = Counter(lot["fee"] for lot in new_lots)
    open_days_new = sum(bool(lot.get("open_days")) for lot in new_lots)
    open_days_matched = sum(bool(item.get("open_days")) for item in analysis["matched"])
    contains_matches = sum(item["match"] == "name-contains+coord" for item in analysis["matched"])
    return {
        "version": 2, "source": {"rows": len(rows), "unique_rsrcNo": len({row.get("rsrcNo") for row in rows}), "raw": str(raw_path.name), "input_sha256": _file_digest(raw_path)},
        "lots_sha256": _source_digest(lots),
        "summary": {"matched": len(analysis["matched"]), "contains_matches": contains_matches, "new": len(new_lots), "review": len(analysis["reviews"]),
                    "free_total": free_total, "free_new": free_new, "free_matched": free_matched,
                    "new_fee": dict(fee_new), "open_days": open_days_new + open_days_matched,
                    "open_days_new": open_days_new, "open_days_matched": open_days_matched,
                    "source_sido": dict(source_counter), "new_sido": dict(accepted_counter), "matched_sido": dict(matched_counter),
                    "review_reasons": dict(Counter(x["reason"] for x in analysis["reviews"]))},
        "matched": analysis["matched"], "new_lots": new_lots,
    }


def apply_candidate(candidate: dict[str, Any], data_path: Path, raw_path: Path, candidate_path: Path, expected_candidate_sha: str) -> None:
    if candidate.get("version") != 2:
        raise RuntimeError("candidate version is not apply-safe")
    if _file_digest(candidate_path) != expected_candidate_sha:
        raise RuntimeError("candidate digest does not match --expected-candidate-sha")
    if candidate.get("source", {}).get("input_sha256") != _file_digest(raw_path):
        raise RuntimeError("raw input changed after review; refusing to apply")
    lock_path = data_path.with_suffix(data_path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        data = json.loads(data_path.read_text())
        if _source_digest(data["lots"]) != candidate["lots_sha256"]:
            raise RuntimeError("data/lots.json changed after candidate generation; refusing to apply")
        matched_items = candidate.get("matched")
        new_lots = candidate.get("new_lots")
        if not isinstance(matched_items, list) or not isinstance(new_lots, list):
            raise RuntimeError("candidate matched/new_lots arrays are required")

        destination_indices: list[int] = []
        incoming_source_ids: list[str] = []
        for item in matched_items:
            raw_index = (item.get("lot") or {}).get("lot_index") if isinstance(item, dict) else None
            if isinstance(raw_index, bool) or not isinstance(raw_index, int):
                raise RuntimeError("candidate contains a non-integer matched lot index")
            try:
                index = raw_index
            except (KeyError, TypeError, ValueError) as exc:
                raise RuntimeError("candidate contains an invalid matched lot index") from exc
            if index < 0 or index >= len(data["lots"]):
                raise RuntimeError(f"matched lot index out of range: {index}")
            destination_indices.append(index)
            source_id = str((item.get("eshare") or {}).get("rsrcNo") or "")
            if not source_id:
                raise RuntimeError("matched candidate is missing eshare.rsrcNo")
            incoming_source_ids.append(source_id)
        for lot in new_lots:
            source_id = str((lot.get("eshare") or {}).get("rsrcNo") or "")
            if not source_id:
                raise RuntimeError("new candidate is missing eshare.rsrcNo")
            incoming_source_ids.append(source_id)
            _validate_relative_output_path(lot.get("path"))

        if len(destination_indices) != len(set(destination_indices)):
            raise RuntimeError("candidate assigns one destination lot more than once")
        if len(incoming_source_ids) != len(set(incoming_source_ids)):
            raise RuntimeError("candidate contains duplicate incoming rsrcNo")

        existing_source_ids: dict[str, list[int]] = defaultdict(list)
        existing_ids: set[str] = set()
        existing_paths: set[str] = set()
        for index, lot in enumerate(data["lots"]):
            existing_ids.add(str(lot.get("id") or ""))
            if lot.get("path"):
                existing_paths.add(canonical_output_path(lot["path"]))
            source = lot.get("eshare")
            if isinstance(source, dict) and source.get("rsrcNo"):
                existing_source_ids[str(source["rsrcNo"])].append(index)
        duplicate_existing_sources = sorted(source for source, indexes in existing_source_ids.items() if len(indexes) > 1)
        if duplicate_existing_sources:
            raise RuntimeError(f"existing lots contain duplicate eshare rsrcNo: {duplicate_existing_sources[:3]}")
        already_present = sorted(set(incoming_source_ids) & set(existing_source_ids))
        if already_present:
            raise RuntimeError(f"candidate rsrcNo already present: {already_present[:3]}")

        new_ids = [str(lot.get("id") or "") for lot in new_lots]
        new_paths = [canonical_output_path(lot.get("path")) for lot in new_lots]
        if any(not value for value in new_ids) or len(new_ids) != len(set(new_ids)):
            raise RuntimeError("candidate contains missing or duplicate new lot ids")
        if len(new_paths) != len(set(new_paths)):
            raise RuntimeError("candidate contains duplicate new lot paths")
        if set(new_ids) & existing_ids:
            raise RuntimeError("candidate new lot id already exists")
        if set(new_paths) & existing_paths:
            raise RuntimeError("candidate new lot path already exists")

        by_index = {item["lot"]["lot_index"]: item for item in matched_items}
        for index, item in by_index.items():
            if not (0 <= index < len(data["lots"])):
                raise RuntimeError(f"matched lot index out of range: {index}")
            lot = data["lots"][index]
            if lot_key(lot) != item["lot"]["lot_key"]:
                raise RuntimeError(f"matched lot fingerprint changed at index {index}")
            if lot.get("eshare"):
                raise RuntimeError(f"matched lot already has eshare data at index {index}")
            if lot.get("eshare_note"):
                raise RuntimeError(f"matched lot already has eshare note at index {index}")
            if lot.get("open_days"):
                raise RuntimeError(f"matched lot already has open_days at index {index}")
            lot["eshare"] = item["eshare"]
            lot["eshare_note"] = item.get("eshare_note", "")
            if item.get("free_open") and not lot.get("free_open"):
                lot["free_open"] = item["free_open"]
            lot["open_days"] = item.get("open_days", "")
        data["lots"].extend(new_lots)
        fd, temp_name = tempfile.mkstemp(prefix="lots.json.", suffix=".tmp", dir=str(data_path.parent))
        try:
            with os.fdopen(fd, "w") as temp:
                temp.write(json.dumps(data, ensure_ascii=False, separators=(",", ":")))
                temp.flush()
                os.fsync(temp.fileno())
            os.replace(temp_name, data_path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _nearest_existing_lot(new_lot: dict[str, Any], existing_lots: list[dict[str, Any]]) -> tuple[float | None, dict[str, Any] | None]:
    lat, lng = parse_coordinate(new_lot.get("lat")), parse_coordinate(new_lot.get("lng"))
    if lat is None or lng is None:
        return None, None
    nearest: tuple[float, dict[str, Any]] | None = None
    for lot in existing_lots:
        other_lat, other_lng = parse_coordinate(lot.get("lat")), parse_coordinate(lot.get("lng"))
        if other_lat is None or other_lng is None:
            continue
        distance = distance_m(lat, lng, other_lat, other_lng)
        if nearest is None or distance < nearest[0]:
            nearest = (distance, lot)
    return nearest if nearest is not None else (None, None)


def write_report(candidate: dict[str, Any], rows: list[dict[str, Any]], existing_lots: list[dict[str, Any]], path: Path) -> None:
    summary = candidate["summary"]
    row_by_id = {str(row["rsrcNo"]): row for row in rows}
    sample_lots = random.Random(20260921).sample(candidate["new_lots"], min(20, len(candidate["new_lots"])))
    sample_checks: list[dict[str, Any]] = []
    for new_lot in sample_lots:
        row = row_by_id[str(new_lot["id"]).removeprefix("eshare-")]
        distance, nearest = _nearest_existing_lot(new_lot, existing_lots)
        expected_fee, _, _ = fee_evidence(html_to_text(row.get("rsrcIntr"), row.get("atpn"), limit=None), str(row.get("freeYn") or "").upper())
        sample_checks.append({"lot": new_lot, "row": row, "distance": distance, "nearest": nearest,
                              "sigungu_ok": bool(nearest and nearest.get("sigungu") == new_lot.get("sigungu")),
                              "fee_expected": expected_fee, "fee_ok": expected_fee == new_lot.get("fee")})
    sigungu_ok_count = sum(check["sigungu_ok"] for check in sample_checks)
    fee_ok_count = sum(check["fee_ok"] for check in sample_checks)
    new_fee = summary.get("new_fee", {})
    lines = ["# 공유누리 개방 주차장 Phase 보고서", "", "- 기준: 2026-09-21 제공 원자료 오프라인 분석", "- API 재호출: 없음", "- 자동 매칭: 정규화 명칭+좌표 50m 또는 정규화 주소(동일 체계) 교집합이 유일한 경우만; 좌표 후보가 유일하고 명칭이 포함 관계인 경우 숫자 토큰 모순이 없을 때 허용", "", "## 요약", "", f"- 원자료: {len(rows):,}건 (`rsrcNo` 유일)", f"- 기존 lot 매칭: {summary['matched']:,}건", f"- 신규 lot: {summary['new']:,}건 (무료 {new_fee.get('무료', 0):,} · 혼합 {new_fee.get('혼합', 0):,} · 유료 {new_fee.get('유료', 0):,})", f"- 검토 큐: {summary['review']:,}건", f"- 무료 원자료: {summary['free_total']:,}건 (신규 {summary['free_new']:,}건, 매칭 {summary['free_matched']:,}건)", f"- 명칭 포함관계 자동 매칭: {summary.get('contains_matches', 0):,}건", f"- `open_days` 보존: {summary.get('open_days', 0):,}건 (신규 {summary.get('open_days_new', 0):,} · 매칭 {summary.get('open_days_matched', 0):,})", "", "## 시도별 분포", "", "| 시도 | 원자료 | 매칭 | 신규 |", "|---|---:|---:|---:|"]
    sidos = sorted(set(summary["source_sido"]) | set(summary["new_sido"]) | set(summary["matched_sido"]))
    for sido in sidos:
        lines.append(f"| {sido} | {summary['source_sido'].get(sido, 0):,} | {summary['matched_sido'].get(sido, 0):,} | {summary['new_sido'].get(sido, 0):,} |")
    lines += ["", "## 검토 사유", "", "| 사유 | 건수 |", "|---|---:|"]
    for reason, count in sorted(summary["review_reasons"].items(), key=lambda x: (-x[1], x[0])):
        lines.append(f"| `{reason}` | {count:,} |")
    lines += ["", "## 무작위 신규 20건 검증", "", f"시드 `20260921`로 신규에서 무작위 추출했습니다. 좌표상 가장 가까운 기존 lot의 시군구 일치 {sigungu_ok_count}/{len(sample_checks)}, 원문 freeYn·요금 신호 기준 fee 분류 일치 {fee_ok_count}/{len(sample_checks)}입니다.", "", "| rsrcNo | 이름 | 주소 | 좌표 | 기관 | 원문 freeYn | 분류 | fee 검증 | 최근접 lot 시군구 | 거리 | 주소 시군구 검증 |", "|---|---|---|---|---|---|---|---|---|---:|---|"]
    for check in sample_checks:
        lot, row, nearest = check["lot"], check["row"], check["nearest"]
        nearest_sigungu = nearest.get("sigungu", "-") if nearest else "-"
        distance = f"{check['distance']:.1f}m" if check["distance"] is not None else "-"
        lines.append(f"| {row['rsrcNo']} | {lot['name']} | {lot['addr']} | {lot['lat']}, {lot['lng']} | {lot['org']} | {str(row.get('freeYn') or '').upper()} | {lot['fee']} | {'Y' if check['fee_ok'] else 'N'} ({check['fee_expected']}) | {nearest_sigungu} | {distance} | {'Y' if check['sigungu_ok'] else 'N'} |")
    lines += ["", "## 제외·보류 기준", "", "- 시도 접두어를 명시적으로 매핑하지 못한 주소는 `unknown_sido_prefix`로 보류했습니다.", "- 한국 좌표 bbox 또는 시도 중심 200km 검증을 통과하지 못한 좌표는 신규로 만들지 않았습니다.", "- 이름·좌표·주소 후보가 있으나 두 근거가 같은 기존 lot을 유일하게 가리키지 않으면 보류했습니다.", "- 기존 lot의 `eshare`가 이미 있거나 여러 공유누리 자원이 한 lot을 차지하려는 경우 덮어쓰지 않고 보류했습니다.", "- 원문 HTML은 태그와 이미지를 제거해 요금 판정에는 전체 텍스트를 사용하고, `eshare_note`에는 최대 600자로 보존했습니다.", "- `freeYn=Y`라도 원문에 유료·양수 금액 신호가 있으면 `혼합`으로 분류해 무료 전용 집계에서 제외했습니다."]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    ap.add_argument("--lots", type=Path, default=ROOT / "data/lots.json")
    ap.add_argument("--candidate", type=Path, default=DEFAULT_CANDIDATE)
    ap.add_argument("--review", type=Path, default=DEFAULT_REVIEW)
    ap.add_argument("--report", type=Path, default=WORKSPACE / "OUTBOX/JUCHABI_ESHARE_PHASE_20260921.md")
    ap.add_argument("--apply-candidate", type=Path, help="apply an already reviewed candidate; never regenerates it")
    ap.add_argument("--expected-candidate-sha", help="required SHA-256 of the reviewed candidate file")
    args = ap.parse_args()
    if args.apply_candidate:
        if not args.expected_candidate_sha:
            raise SystemExit("--expected-candidate-sha is required with --apply-candidate")
        candidate = json.loads(args.apply_candidate.read_text())
        apply_candidate(candidate, args.lots, args.input, args.apply_candidate, args.expected_candidate_sha)
        print(json.dumps({"applied": True, **candidate["summary"]}, ensure_ascii=False, indent=2))
        return
    data = json.loads(args.lots.read_text())
    rows, detail_rows = load_raw(args.input)
    if len(rows) != len(detail_rows):
        raise RuntimeError("list/detail row count mismatch after merge")
    analysis = analyze_rows(rows, data["lots"])
    candidate = build_candidate(data, rows, analysis, args.input)
    args.candidate.parent.mkdir(parents=True, exist_ok=True)
    args.candidate.write_text(json.dumps(candidate, ensure_ascii=False, indent=2))
    args.review.parent.mkdir(parents=True, exist_ok=True)
    args.review.write_text(json.dumps(analysis["reviews"], ensure_ascii=False, indent=2))
    write_report(candidate, rows, data["lots"], args.report)
    print(json.dumps(candidate["summary"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
