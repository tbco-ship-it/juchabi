#!/usr/bin/env python3
"""Resumable ShareNuri list/detail fetcher for future refreshes.

This command is not run as part of the 2026-09-21 integration because the
owner supplied a complete raw artifact and explicitly prohibited re-fetching.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

import fcntl


ROOT = Path(__file__).resolve().parent.parent
LIST_URL = "https://www.eshare.go.kr/eshare-openapi/rsrc/list/010700/{key}"
DETAIL_URL = "https://www.eshare.go.kr/eshare-openapi/rsrc/detail/{key}"
DEFAULT_OUT = ROOT / "data/raw/eshare"
KST = ZoneInfo("Asia/Seoul")
MAX_DAILY_CALLS = 1000
MANIFEST_NAME = "manifest.json"


def today_kst() -> str:
    return dt.datetime.now(KST).date().isoformat()


def _budget_path(out_dir: Path) -> Path:
    return out_dir / "budget.json"


def _budget_lock_path(out_dir: Path) -> Path:
    return out_dir / "budget.json.lock"


def _validate_budget_limit(limit: int) -> int:
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_DAILY_CALLS:
        raise ValueError(f"budget must be between 1 and {MAX_DAILY_CALLS}")
    return limit


def _atomic_write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as temp:
            temp.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
            temp.flush()
            os.fsync(temp.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _read_budget_unlocked(out_dir: Path, limit: int) -> dict[str, int | str]:
    path = _budget_path(out_dir)
    if path.exists():
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("budget ledger is corrupt; refusing to spend calls") from exc
        if not isinstance(data, dict):
            raise RuntimeError("budget ledger is not an object")
        stored_limit = data.get("limit", limit)
        try:
            stored_limit = int(stored_limit)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("budget ledger has an invalid limit") from exc
        _validate_budget_limit(stored_limit)
        if stored_limit != limit:
            raise RuntimeError(f"budget limit mismatch: ledger={stored_limit}, requested={limit}")
    else:
        data = {"date": today_kst(), "calls": 0, "limit": limit}
    if data.get("date") != today_kst():
        # A new calendar day gets a new allowance; changing snapshots on the
        # same day never resets this counter.
        data = {"date": today_kst(), "calls": 0, "limit": limit}
    calls = data.get("calls", 0)
    if not isinstance(calls, int) or isinstance(calls, bool) or calls < 0:
        raise RuntimeError("budget ledger has an invalid call count")
    data["limit"] = limit
    if calls > limit:
        raise RuntimeError("budget ledger exceeds its daily limit")
    return data


def load_budget(out_dir: Path, limit: int) -> dict[str, int | str]:
    limit = _validate_budget_limit(limit)
    out_dir.mkdir(parents=True, exist_ok=True)
    with _budget_lock_path(out_dir).open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        data = _read_budget_unlocked(out_dir, limit)
        _atomic_write_json(_budget_path(out_dir), data)
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    return data


def save_budget(out_dir: Path, budget: dict[str, int | str]) -> None:
    limit = _validate_budget_limit(int(budget.get("limit", 0)))
    out_dir.mkdir(parents=True, exist_ok=True)
    with _budget_lock_path(out_dir).open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        data = dict(budget)
        data["limit"] = limit
        _atomic_write_json(_budget_path(out_dir), data)
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _reserve_call(out_dir: Path, limit: int) -> dict[str, int | str]:
    limit = _validate_budget_limit(limit)
    out_dir.mkdir(parents=True, exist_ok=True)
    with _budget_lock_path(out_dir).open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        data = _read_budget_unlocked(out_dir, limit)
        if int(data["calls"]) >= limit:
            raise RuntimeError(f"daily ShareNuri call budget exhausted ({limit})")
        data["calls"] = int(data["calls"]) + 1
        _atomic_write_json(_budget_path(out_dir), data)
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    return data


def _assert_success_envelope(payload: object) -> None:
    if not isinstance(payload, dict):
        return
    if any(payload.get(key) not in (None, "", False, 0, [], {}) for key in ("error", "errors")):
        raise RuntimeError("ShareNuri returned an error envelope")
    if payload.get("success") is False:
        raise RuntimeError("ShareNuri returned success=false")
    success_codes = {"0", "00", "200", "ok", "success", "normal", "normal_code", "normal_service"}
    for key in ("resultCode", "result_code", "code", "status"):
        if key not in payload or payload[key] in (None, ""):
            continue
        value = str(payload[key]).strip().lower()
        if value not in success_codes and not (value.isdigit() and int(value) in {0, 200}):
            raise RuntimeError(f"ShareNuri returned non-success {key}={payload[key]!r}")
    for key in ("resultMsg", "result_msg", "message", "msg"):
        value = str(payload.get(key) or "").lower()
        if any(word in value for word in ("invalid request", "error", "failed", "failure")):
            raise RuntimeError(f"ShareNuri returned error message in {key}")
    for value in payload.values():
        if isinstance(value, dict):
            _assert_success_envelope(value)


def _find_item_list(payload: object) -> list[object] | None:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return None
    for key in ("items", "item", "list", "data"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            found = _find_item_list(value)
            if found is not None:
                return found
    for value in payload.values():
        found = _find_item_list(value)
        if found is not None:
            return found
    return None


def extract_items(payload: object) -> list[dict]:
    _assert_success_envelope(payload)
    found = _find_item_list(payload)
    if found is None:
        raise RuntimeError("ShareNuri response did not contain an item list")
    if any(not isinstance(item, dict) for item in found):
        raise RuntimeError("ShareNuri item list contains a non-object")
    return list(found)  # type: ignore[arg-type]


def _quarantine_cache(path: Path) -> None:
    stamp = int(time.time() * 1000)
    target = path.with_name(f"{path.name}.corrupt-{stamp}")
    path.replace(target)


def _cached_items(path: Path) -> list[dict] | None:
    try:
        return extract_items(json.loads(path.read_text()))
    except (OSError, json.JSONDecodeError, RuntimeError, TypeError, ValueError):
        _quarantine_cache(path)
        return None


def _validate_rows(rows: list[dict], *, label: str, expected_ids: set[str] | None = None, max_rows: int | None = None) -> None:
    if max_rows is not None and len(rows) > max_rows:
        raise RuntimeError(f"{label} returned more than requested rows")
    ids = [str(row.get("rsrcNo") or "") for row in rows]
    if any(not value for value in ids) or len(set(ids)) != len(ids):
        raise RuntimeError(f"{label} contains missing or duplicate rsrcNo")
    if expected_ids is not None and set(ids) != expected_ids:
        raise RuntimeError(f"{label} does not cover exactly the requested rsrcNo set")


def _request_config(page_size: int) -> dict[str, object]:
    return {"date": today_kst(), "page_size": page_size, "detail_batch_size": 100,
            "list_body": ["pageNo", "numOfRows"], "detail_body": ["rsrcNoList"]}


def _prepare_manifest(out_dir: Path, page_size: int, fresh_snapshot: bool = False) -> dict[str, object]:
    config = _request_config(page_size)
    fingerprint = hashlib.sha256(json.dumps(config, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    path = out_dir / MANIFEST_NAME
    if path.exists():
        try:
            manifest = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("fetch manifest is corrupt; refusing to resume") from exc
        if manifest.get("request_fingerprint") != fingerprint or fresh_snapshot:
            if not fresh_snapshot:
                raise RuntimeError("fetch cache parameters changed; use --new-snapshot or a new out-dir")
            old_id = re.sub(r"[^A-Za-z0-9_.-]", "-", str(manifest.get("snapshot_id") or "old"))
            archive = out_dir / "snapshots" / old_id
            suffix = 2
            while archive.exists():
                archive = out_dir / "snapshots" / f"{old_id}-{suffix}"
                suffix += 1
            archive.mkdir(parents=True, exist_ok=False)
            for cached in list(out_dir.glob("list-page-*.json")) + list(out_dir.glob("detail-batch-*.json")) + list(out_dir.glob("eshare_*.json")) + [path]:
                if cached.exists():
                    cached.replace(archive / cached.name)
    else:
        stale = list(out_dir.glob("list-page-*.json")) + list(out_dir.glob("detail-batch-*.json"))
        if stale and not fresh_snapshot:
            raise RuntimeError("fetch cache has no manifest; refusing to resume unbound files")
        if stale and fresh_snapshot:
            archive = out_dir / "snapshots" / f"unbound-{int(time.time())}"
            archive.mkdir(parents=True, exist_ok=False)
            for cached in stale:
                cached.replace(archive / cached.name)
    manifest = {"version": 1, "snapshot_id": f"{config['date']}-{fingerprint[:12]}",
                "request_fingerprint": fingerprint, **config}
    _atomic_write_json(path, manifest)
    return manifest


def post_json(url: str, body: dict, timeout: float = 60) -> object:
    request = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers={"Content-Type": "application/json", "Accept": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def api_call(url: str, body: dict, out_dir: Path, budget: dict[str, int | str], timeout: float) -> object:
    reserved = _reserve_call(out_dir, int(budget["limit"]))
    budget.update(reserved)
    try:
        return post_json(url, body, timeout=timeout)
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise RuntimeError(f"ShareNuri request failed: {type(exc).__name__}") from exc


def fetch_all(key: str, out_dir: Path, page_size: int, budget_limit: int, timeout: float, pause: float,
              fresh_snapshot: bool = False) -> dict:
    if not isinstance(page_size, int) or isinstance(page_size, bool) or not 1 <= page_size <= 100:
        raise ValueError("page_size must be between 1 and 100")
    _validate_budget_limit(budget_limit)
    out_dir.mkdir(parents=True, exist_ok=True)
    _prepare_manifest(out_dir, page_size, fresh_snapshot=fresh_snapshot)
    budget = load_budget(out_dir, budget_limit)
    list_rows: list[dict] = []
    page = 1
    while True:
        cache = out_dir / f"list-page-{page:04d}.json"
        if cache.exists():
            rows = _cached_items(cache)
            if rows is None:
                rows = []
                cache_missing = True
            else:
                try:
                    _validate_rows(rows, label=f"cached list page {page}", max_rows=page_size)
                    if page == 1 and not rows:
                        raise RuntimeError("list page 1 cache is empty")
                except RuntimeError:
                    _quarantine_cache(cache)
                    rows = []
                    cache_missing = True
                else:
                    cache_missing = False
        else:
            rows = []
            cache_missing = True
        if cache_missing:
            payload = api_call(LIST_URL.format(key=key), {"pageNo": page, "numOfRows": page_size}, out_dir, budget, timeout)
            rows = extract_items(payload)
            _validate_rows(rows, label=f"list page {page}", max_rows=page_size)
            _atomic_write_json(cache, rows)
        else:
            _validate_rows(rows, label=f"cached list page {page}", max_rows=page_size)
        if page == 1 and not rows:
            raise RuntimeError("list page 1 returned no resources")
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
            rows = _cached_items(cache)
            if rows is None:
                rows = []
                cache_missing = True
            else:
                try:
                    _validate_rows(rows, label=f"cached detail batch {batch_no}", expected_ids=set(batch_ids), max_rows=100)
                except RuntimeError:
                    _quarantine_cache(cache)
                    rows = []
                    cache_missing = True
                else:
                    cache_missing = False
        else:
            rows = []
            cache_missing = True
        if cache_missing:
            payload = api_call(DETAIL_URL.format(key=key), {"rsrcNoList": batch_ids}, out_dir, budget, timeout)
            rows = extract_items(payload)
            _validate_rows(rows, label=f"detail batch {batch_no}", expected_ids=set(batch_ids), max_rows=100)
            _atomic_write_json(cache, rows)
        else:
            _validate_rows(rows, label=f"cached detail batch {batch_no}", expected_ids=set(batch_ids), max_rows=100)
        detail_rows.extend(rows)
        if pause:
            time.sleep(pause)
    detail_ids = [str(row.get("rsrcNo") or "") for row in detail_rows]
    if len(detail_ids) != len(ids) or len(set(detail_ids)) != len(detail_ids) or set(detail_ids) != set(ids):
        raise RuntimeError("detail response does not cover exactly the list rsrcNo set")
    result = {"fetched": today_kst().replace("-", ""), "list": list_rows, "detail": detail_rows}
    output = out_dir / f"eshare_{result['fetched']}.json"
    _atomic_write_json(output, result)
    current_budget = load_budget(out_dir, budget_limit)
    return {"output": str(output), "list": len(list_rows), "detail": len(detail_rows), "calls": current_budget["calls"]}


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
    ap.add_argument("--new-snapshot", action="store_true", help="archive incompatible cache files before starting a fresh snapshot")
    ap.add_argument("--validate-raw", type=Path)
    args = ap.parse_args()
    _validate_budget_limit(args.budget)
    if args.validate_raw:
        print(json.dumps(validate_raw(args.validate_raw), ensure_ascii=False, indent=2))
        return
    if not args.key:
        raise SystemExit("ESHARE_API_KEY is required for a network fetch")
    print(json.dumps(fetch_all(args.key, args.out_dir, args.page_size, args.budget, args.timeout, args.pause, args.new_snapshot), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
