#!/usr/bin/env python3
"""Resumable ShareNuri list/detail fetcher for future refreshes.

This command is not run as part of the 2026-09-21 integration because the
owner supplied a complete raw artifact and explicitly prohibited re-fetching.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parent.parent
LIST_URL = "https://www.eshare.go.kr/eshare-openapi/rsrc/list/010700/{key}"
DETAIL_URL = "https://www.eshare.go.kr/eshare-openapi/rsrc/detail/{key}"
DEFAULT_OUT = ROOT / "data/raw/eshare"
KST = ZoneInfo("Asia/Seoul")


def today_kst() -> str:
    return dt.datetime.now(KST).date().isoformat()


def _budget_path(out_dir: Path) -> Path:
    return out_dir / "budget.json"


def load_budget(out_dir: Path, limit: int) -> dict[str, int | str]:
    path = _budget_path(out_dir)
    if path.exists():
        data = json.loads(path.read_text())
    else:
        data = {"date": today_kst(), "calls": 0, "limit": limit}
    if data.get("date") != today_kst():
        data = {"date": today_kst(), "calls": 0, "limit": limit}
    data["limit"] = limit
    return data


def save_budget(out_dir: Path, budget: dict[str, int | str]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = _budget_path(out_dir)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(budget, ensure_ascii=False, indent=2) + "\n")
    temp.replace(path)


def extract_items(payload: object) -> list[dict]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("items", "item", "list", "data"):
        value = payload.get(key)
        if isinstance(value, list):
            return [x for x in value if isinstance(x, dict)]
        if isinstance(value, dict):
            found = extract_items(value)
            if found:
                return found
    for value in payload.values():
        found = extract_items(value)
        if found:
            return found
    return []


def post_json(url: str, body: dict, timeout: float = 60) -> object:
    request = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers={"Content-Type": "application/json", "Accept": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def api_call(url: str, body: dict, out_dir: Path, budget: dict[str, int | str], timeout: float) -> object:
    if int(budget["calls"]) >= int(budget["limit"]):
        raise RuntimeError(f"daily ShareNuri call budget exhausted ({budget['limit']})")
    budget["calls"] = int(budget["calls"]) + 1
    save_budget(out_dir, budget)
    try:
        return post_json(url, body, timeout=timeout)
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise RuntimeError(f"ShareNuri request failed: {type(exc).__name__}") from exc


def fetch_all(key: str, out_dir: Path, page_size: int, budget_limit: int, timeout: float, pause: float) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    budget = load_budget(out_dir, budget_limit)
    list_rows: list[dict] = []
    page = 1
    while True:
        cache = out_dir / f"list-page-{page:04d}.json"
        if cache.exists():
            rows = json.loads(cache.read_text())
        else:
            payload = api_call(LIST_URL.format(key=key), {"pageNo": page, "numOfRows": page_size}, out_dir, budget, timeout)
            rows = extract_items(payload)
            cache.write_text(json.dumps(rows, ensure_ascii=False, indent=2))
        list_rows.extend(rows)
        if len(rows) < page_size:
            break
        page += 1
        if pause:
            time.sleep(pause)
    ids = [str(row.get("rsrcNo") or "") for row in list_rows]
    if not all(ids) or len(set(ids)) != len(ids):
        raise RuntimeError("list response has missing or duplicate rsrcNo")
    detail_rows: list[dict] = []
    for batch_no, start in enumerate(range(0, len(ids), 100), 1):
        batch_ids = ids[start:start + 100]
        cache = out_dir / f"detail-batch-{batch_no:04d}.json"
        if cache.exists():
            rows = json.loads(cache.read_text())
        else:
            payload = api_call(DETAIL_URL.format(key=key), {"rsrcNoList": batch_ids}, out_dir, budget, timeout)
            rows = extract_items(payload)
            cache.write_text(json.dumps(rows, ensure_ascii=False, indent=2))
        detail_rows.extend(rows)
        if pause:
            time.sleep(pause)
    if {str(row.get("rsrcNo") or "") for row in detail_rows} != set(ids):
        raise RuntimeError("detail response does not cover exactly the list rsrcNo set")
    result = {"fetched": today_kst().replace("-", ""), "list": list_rows, "detail": detail_rows}
    output = out_dir / f"eshare_{result['fetched']}.json"
    output.write_text(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return {"output": str(output), "list": len(list_rows), "detail": len(detail_rows), "calls": budget["calls"]}


def validate_raw(path: Path) -> dict[str, int]:
    data = json.loads(path.read_text())
    lists, details = data.get("list", []), data.get("detail", [])
    list_ids = [str(row.get("rsrcNo") or "") for row in lists]
    ids = [str(row.get("rsrcNo") or "") for row in details]
    if not list_ids or not all(list_ids) or len(set(list_ids)) != len(list_ids):
        raise RuntimeError("raw list rows must have unique rsrcNo")
    if not ids or not all(ids) or len(set(ids)) != len(ids):
        raise RuntimeError("raw detail rows must have unique rsrcNo")
    if set(list_ids) != set(ids):
        raise RuntimeError("raw list/detail rows must cover the same rsrcNo set")
    return {"list": len(lists), "detail": len(details), "unique_rsrcNo": len(set(ids))}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", default=os.environ.get("ESHARE_API_KEY"))
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--page-size", type=int, default=100)
    ap.add_argument("--budget", type=int, default=1000)
    ap.add_argument("--timeout", type=float, default=60)
    ap.add_argument("--pause", type=float, default=0.2)
    ap.add_argument("--validate-raw", type=Path)
    args = ap.parse_args()
    if args.validate_raw:
        print(json.dumps(validate_raw(args.validate_raw), ensure_ascii=False, indent=2))
        return
    if not args.key:
        raise SystemExit("ESHARE_API_KEY is required for a network fetch")
    print(json.dumps(fetch_all(args.key, args.out_dir, args.page_size, args.budget, args.timeout, args.pause), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
