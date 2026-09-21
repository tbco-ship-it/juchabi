#!/usr/bin/env python3
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from normalize_eshare import (  # noqa: E402
    analyze_rows,
    canonical_output_path,
    _new_lot,
    _source_digest,
    apply_candidate,
    assign_new_paths,
    address_key,
    address_system,
    classify_sido,
    fee_evidence,
    geography_status,
    html_to_text,
    load_raw,
    normalize_name,
    numeric_name_conflict,
    open_days_from_values,
    safe_eshare_url,
    source_name_text,
)
from build import (  # noqa: E402
    ESHARE_WINDOW_FREE_TEXT,
    canonical_output_path as build_canonical_output_path,
    is_time_window_free_open,
    weekend_cells,
)
import fetch_eshare as fetch_eshare_module  # noqa: E402
from fetch_eshare import (  # noqa: E402
    TRANSITION_NAME,
    _prepare_manifest,
    _validate_budget_limit,
    extract_items,
    load_budget,
)


def source(**overrides):
    row = {
        "rsrcNo": "TEST-1", "rsrcNm": "알파 공영주차장", "rsrcClsNm": "부설", "rsrcClsCd": "010703",
        "rsrcInstNm": "서울특별시 알파구청", "freeYn": "Y", "rsrcIntr": "<p>무료<br>수용인원: 20대</p>",
        "atpn": "<img src='secret'>주의사항", "addr": "서울특별시 알파구 테헤란로 10", "lat": 37.5, "lot": 127.0,
        "instUrlAddr": "https://www.eshare.go.kr/detail/TEST-1", "updYmd": "2025-12-12",
    }
    row.update(overrides)
    return row


def lot(**overrides):
    row = {
        "id": "legacy-1", "name": "알파", "addr": "서울특별시 알파구 테헤란로 10", "addr_parcel": "서울특별시 알파구 알파동 1-2",
        "sido": "서울특별시", "sido_slug": "seoul", "sigungu": "알파구", "lat": 37.5, "lng": 127.0,
    }
    row.update(overrides)
    return row


class EsharePipelineTests(unittest.TestCase):
    def test_html_is_text_only_and_keeps_lines(self):
        text = html_to_text("<p>무료<br>사용료: 0원</p><script>drop()</script><img src='x'>", "<div>주의</div>")
        self.assertEqual(text, "무료\n사용료: 0원\n주의")
        self.assertNotIn("drop", text)

    def test_name_and_address_normalization(self):
        self.assertEqual(normalize_name("알파 공영주차장(본관)"), "알파본관")
        self.assertEqual(address_system("서울특별시 알파구 테헤란로 10"), "road")
        self.assertIsNone(address_system("서울특별시 알파구 마들로"))
        self.assertEqual(address_key("서울특별시 알파구 알파동 1-2 일대"), ("parcel", "서울특별시알파구알파동1-2"))

    def test_explicit_sido_mapping_and_unknown_prefix(self):
        self.assertEqual(classify_sido("경기 광명시 시청로 20"), ("경기도", "gyeonggi", "광명시"))
        self.assertEqual(classify_sido("광주 북구 무등로 1"), ("전남광주통합특별시", "gwangju-jeonnam", "광주 북구"))
        self.assertEqual(classify_sido("삽교읍 신가리 264-4"), (None, None, "unknown_sido_prefix"))

    def test_name_plus_coordinate_is_singleton_match(self):
        result = analyze_rows([source()], [lot()])
        self.assertEqual(len(result["matched"]), 1)
        self.assertEqual(result["matched"][0]["match"], "name+coord")
        self.assertIn("무료", result["matched"][0]["eshare_note"])
        self.assertFalse(result["new_lots"])
        self.assertFalse(result["reviews"])

    def test_name_plus_address_matches_even_when_coordinate_is_far(self):
        result = analyze_rows([source(lat=37.6, lot=127.1)], [lot()])
        self.assertEqual(len(result["matched"]), 1)
        self.assertEqual(result["matched"][0]["match"], "name+address")

    def test_valid_unmatched_source_becomes_open_lot_with_null_hours(self):
        result = analyze_rows([source(rsrcNo="TEST-2", rsrcNm="새 기관 주차장", addr="부산광역시 해운대구 해운대로 10", lat=35.16, lot=129.16)], [])
        self.assertEqual(len(result["new_lots"]), 1)
        generated = result["new_lots"][0]
        self.assertEqual(generated["hours"], None)
        self.assertEqual(generated["kind"], "개방")
        self.assertEqual(generated["type"], "부설")
        self.assertEqual(generated["fee"], "무료")
        self.assertEqual(generated["provider"], "공유누리")

    def test_free_window_paid_signal_becomes_mixed_and_paid_code_is_paid(self):
        self.assertEqual(fee_evidence("평일 09~18시 유료, 최초 30분 600원", "Y")[:2], ("혼합", True))
        self.assertEqual(fee_evidence("사용료: 없음", "Y")[:2], ("무료", False))
        self.assertEqual(fee_evidence("기관 안내 참조", "N")[:2], ("유료", True))

    def test_open_days_preserve_three_raw_values_without_parsing(self):
        self.assertEqual(
            open_days_from_values("<평일개방> 미개방 <토요일개방> 09:00~18:00 <휴일개방> 미개방"),
            "평일 미개방 · 토 09:00~18:00 · 휴일 미개방",
        )

    def test_name_containment_with_single_coordinate_is_accepted(self):
        result = analyze_rows(
            [source(rsrcNm="알파 제1 주차장", rsrcIntr="<p>무료</p>")],
            [lot(name="알파 제1 주차장(본관)")],
        )
        self.assertEqual(len(result["matched"]), 1)
        self.assertEqual(result["matched"][0]["match"], "name-contains+coord")

    def test_numeric_name_conflict_is_not_a_containment_match(self):
        self.assertTrue(numeric_name_conflict("신당동 1공영", "신당동 2공영"))
        self.assertFalse(numeric_name_conflict("신당동 1공영", "신당동 1공영 주차장"))
        self.assertTrue(numeric_name_conflict("101동 제1 주차장", "101동 제12 주차장"))

    def test_provider_address_tail_does_not_become_name_number_conflict(self):
        row = source(
            rsrcNm="내죽도공원 제1노상 주차장 통영시 광도면 죽림리 1574-42번지",
            addr="경남 통영시 광도면 죽림리 1574-42",
        )
        self.assertEqual(source_name_text(row), "내죽도공원 제1노상 주차장")

    def test_generic_daddr_cannot_hide_primary_name_number_conflict(self):
        result = analyze_rows(
            [source(
                rsrcNo="NUMBER-CONFLICT",
                rsrcNm="알파 제12 주차장",
                daddr="알파 주차장",
                addr="서울특별시 알파구 테헤란로 11",
            )],
            [lot(name="알파 제1 주차장")],
        )
        self.assertFalse(result["matched"])
        self.assertFalse(result["new_lots"])
        self.assertEqual(result["reviews"][0]["reason"], "insufficient_or_conflicting_evidence")

    def test_zero_won_is_not_paid_evidence_but_positive_amount_is(self):
        for note in ("사용료: 0원", "사용료: 0 원", "사용료: 0.0원"):
            self.assertEqual(fee_evidence(note, "Y")[:2], ("무료", False))
        self.assertEqual(fee_evidence("최초 30분 0원, 이후 1,000원", "Y")[:2], ("혼합", True))

    def test_zero_allowance_does_not_hide_later_charge_clause(self):
        for note in (
            "최초 30분 0원, 이후 주차요금 부과",
            "최초 30분 0원\n이후 주차요금 부과",
        ):
            fee, reviewed, raw = fee_evidence(note, "Y")
            self.assertEqual((fee, reviewed), ("혼합", True))
            self.assertIn("요금 부과", raw["공유누리"])
        self.assertEqual(fee_evidence("사용료: 없음", "Y")[:2], ("무료", False))

    def test_paid_signal_after_display_limit_is_classified(self):
        long_note = "무료 안내 " * 100 + "평일 09~18시 유료, 최초 30분 600원"
        generated = _new_lot(
            source(rsrcNo="LONG-1", rsrcIntr=long_note, atpn=""),
            geography_status(source()),
        )
        self.assertEqual(generated["fee"], "혼합")
        self.assertLessEqual(len(generated["eshare_note"]), 600)

    def test_suspicious_url_forms_and_sigungu_are_rejected(self):
        self.assertEqual(safe_eshare_url("https://outside.example\\@www.eshare.go.kr/detail/1"), "")
        self.assertEqual(safe_eshare_url("https://user:pass@www.eshare.go.kr/detail/1"), "")
        self.assertEqual(classify_sido("서울 ../../../outside구 1"), ("서울특별시", "seoul", "bad_sigungu"))

    def test_share_nuri_free_does_not_infer_weekend_cells(self):
        self.assertEqual(
            weekend_cells({"fee": "무료", "id": "eshare-1", "eshare": {"free": "Y"}, "open_days": "평일 미개방 · 토 09:00~18:00 · 휴일 미개방"}),
            {"sat": "미확인", "hol": "미확인"},
        )

    def test_matched_share_nuri_metadata_preserves_legacy_weekend_evidence(self):
        self.assertEqual(
            weekend_cells({
                "fee": "유료", "id": "legacy-1", "eshare": {"free": "Y"},
                "open_days": "평일 10:00~14:00 · 토 미개방 · 휴일 미개방",
                "seoul_code": "123", "sat_free": False, "hol_free": True,
            }),
            {"sat": "유료", "hol": "무료"},
        )

    def test_generic_share_nuri_free_window_is_not_night_weekend_evidence(self):
        self.assertFalse(is_time_window_free_open({"eshare": {"rsrcNo": "1"}, "free_open": ESHARE_WINDOW_FREE_TEXT}))
        self.assertTrue(is_time_window_free_open({"free_open": "주말·공휴일 무료 개방"}))

    def test_assign_new_paths_reserves_final_slug(self):
        existing = [lot(slug="foo"), lot(id="legacy-2", slug="foo-2")]
        incoming = [
            {"id": "eshare-2", "name": "foo", "sido_slug": "seoul", "sigungu": "알파구"},
            {"id": "eshare-1", "name": "foo", "sido_slug": "seoul", "sigungu": "알파구"},
        ]
        assign_new_paths(existing, incoming)
        self.assertEqual({row["slug"] for row in incoming}, {"foo-3", "foo-4"})

    def test_error_envelope_and_missing_item_list_are_not_empty_success(self):
        with self.assertRaisesRegex(RuntimeError, "error envelope"):
            extract_items({"error": "INVALID REQUEST", "items": []})
        with self.assertRaisesRegex(RuntimeError, "non-success"):
            extract_items({"response": {"header": {"resultCode": "03"}, "body": {"items": []}}})
        with self.assertRaisesRegex(RuntimeError, "item list"):
            extract_items({"resultCode": "00", "resultMsg": "NORMAL_CODE"})

    def test_fetch_manifest_binds_resume_parameters_and_budget_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            out_dir = Path(directory)
            first = _prepare_manifest(out_dir, 100)
            with self.assertRaisesRegex(RuntimeError, "parameters changed"):
                _prepare_manifest(out_dir, 50)
            second = _prepare_manifest(out_dir, 50, fresh_snapshot=True)
            self.assertNotEqual(first["request_fingerprint"], second["request_fingerprint"])
            self.assertTrue(list((out_dir / "snapshots").iterdir()))
            self.assertEqual(load_budget(out_dir, 1000)["calls"], 0)
            with self.assertRaises(ValueError):
                _validate_budget_limit(1001)

    def test_interrupted_snapshot_rollover_fails_closed_and_recovers_fresh(self):
        with tempfile.TemporaryDirectory() as directory:
            out_dir = Path(directory)
            _prepare_manifest(out_dir, 100)
            (out_dir / "list-page-0001.json").write_text(json.dumps([{"rsrcNo": "R", "freeYn": "Y"}]))
            (out_dir / "detail-batch-0001.json").write_text(json.dumps([{"rsrcNo": "R", "freeYn": "Y"}]))
            (out_dir / "eshare_20260921.json").write_text(json.dumps({"list": [{"rsrcNo": "R", "freeYn": "Y"}], "detail": [{"rsrcNo": "R", "freeYn": "Y"}]}))

            def move_one_then_fail(path, archive_name):
                archive = path / "snapshots" / archive_name
                archive.mkdir(parents=True, exist_ok=True)
                files = fetch_eshare_module._snapshot_files(path)
                files[0].replace(archive / files[0].name)
                raise OSError("simulated interruption")

            with patch.object(fetch_eshare_module, "_archive_snapshot_files", side_effect=move_one_then_fail):
                with self.assertRaises(OSError):
                    _prepare_manifest(out_dir, 100, fresh_snapshot=True)
            self.assertTrue((out_dir / TRANSITION_NAME).exists())
            with self.assertRaisesRegex(RuntimeError, "transition is incomplete"):
                _prepare_manifest(out_dir, 100)

            _prepare_manifest(out_dir, 100, fresh_snapshot=True)
            self.assertFalse((out_dir / TRANSITION_NAME).exists())
            self.assertTrue((out_dir / "manifest.json").exists())
            self.assertFalse((out_dir / "list-page-0001.json").exists())
            self.assertFalse((out_dir / "detail-batch-0001.json").exists())
            archived = list((out_dir / "snapshots").glob("*/list-page-0001.json"))
            self.assertEqual(len(archived), 1)

    def test_fetch_manifest_and_transition_marker_reject_malformed_state(self):
        with tempfile.TemporaryDirectory() as directory:
            out_dir = Path(directory)
            (out_dir / "manifest.json").write_text(json.dumps(["not", "an", "object"]))
            with self.assertRaisesRegex(RuntimeError, "manifest is not an object"):
                _prepare_manifest(out_dir, 100)

            (out_dir / "manifest.json").unlink()
            (out_dir / TRANSITION_NAME).write_text(json.dumps({"archive_name": "../escape"}))
            with self.assertRaisesRegex(RuntimeError, "archive name is unsafe"):
                _prepare_manifest(out_dir, 100, fresh_snapshot=True)

    def test_apply_rejects_duplicate_destinations_before_dict_comprehension(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data_path = root / "lots.json"
            raw_path = root / "raw.json"
            candidate_path = root / "candidate.json"
            data = {"source": {}, "lots": [lot(path="seoul/알파구/알파/")]}
            data_path.write_text(json.dumps(data, ensure_ascii=False))
            raw_path.write_text(json.dumps({"list": [{"rsrcNo": "1"}], "detail": [{"rsrcNo": "1"}]}))
            candidate = {
                "version": 2,
                "source": {"input_sha256": __import__("hashlib").sha256(raw_path.read_bytes()).hexdigest()},
                "lots_sha256": _source_digest(data["lots"]),
                "matched": [
                    {"lot": {"lot_index": 0, "lot_key": lot(path="seoul/알파구/알파/") and "legacy-1|알파|서울특별시 알파구 알파동 1-2"}, "eshare": {"rsrcNo": "1"}},
                    {"lot": {"lot_index": 0, "lot_key": "legacy-1|알파|서울특별시 알파구 알파동 1-2"}, "eshare": {"rsrcNo": "2"}},
                ],
                "new_lots": [],
            }
            candidate_path.write_text(json.dumps(candidate, ensure_ascii=False))
            with self.assertRaisesRegex(RuntimeError, "more than once"):
                apply_candidate(candidate, data_path, raw_path, candidate_path, __import__("hashlib").sha256(candidate_path.read_bytes()).hexdigest())

    def test_apply_rejects_dot_segment_and_preserves_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data_path = root / "lots.json"
            raw_path = root / "raw.json"
            candidate_path = root / "candidate.json"
            data = {"source": {}, "lots": [lot(path="seoul/알파구/알파/")]}
            data_path.write_text(json.dumps(data, ensure_ascii=False))
            raw_path.write_text(json.dumps({"list": [{"rsrcNo": "1"}], "detail": [{"rsrcNo": "1"}]}))
            candidate = {
                "version": 2,
                "source": {"input_sha256": __import__("hashlib").sha256(raw_path.read_bytes()).hexdigest()},
                "lots_sha256": _source_digest(data["lots"]),
                "matched": [],
                "new_lots": [{"id": "eshare-1", "path": "seoul/알파구/./알파/", "eshare": {"rsrcNo": "1"}}],
            }
            candidate_path.write_text(json.dumps(candidate, ensure_ascii=False))
            before = data_path.read_bytes()
            with self.assertRaisesRegex(RuntimeError, "unsafe relative output path"):
                apply_candidate(candidate, data_path, raw_path, candidate_path, __import__("hashlib").sha256(candidate_path.read_bytes()).hexdigest())
            self.assertEqual(data_path.read_bytes(), before)

    def test_output_route_identity_is_slash_canonical(self):
        self.assertEqual(canonical_output_path("seoul/알파구/알파/"), "seoul/알파구/알파/")
        self.assertEqual(build_canonical_output_path("seoul/알파구/알파"), build_canonical_output_path("seoul/알파구/알파/"))

    def test_source_url_is_https_and_host_bound(self):
        self.assertEqual(safe_eshare_url("https://www.eshare.go.kr/detail/1"), "https://www.eshare.go.kr/detail/1")
        self.assertEqual(safe_eshare_url("http://www.eshare.go.kr/detail/1"), "")
        self.assertEqual(safe_eshare_url("https://evil.example/detail/1"), "")
        self.assertEqual(safe_eshare_url("javascript:alert(1)"), "")

    def test_raw_list_and_detail_sets_must_match(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "raw.json"
            path.write_text(json.dumps({"list": [{"rsrcNo": "1"}], "detail": [{"rsrcNo": "2"}]}))
            with self.assertRaisesRegex(ValueError, "sets differ"):
                load_raw(path)

    def test_invalid_geography_is_review_not_new(self):
        result = analyze_rows([source(addr="삽교읍 신가리 264-4", lat=36.7, lot=126.8)], [])
        self.assertFalse(result["new_lots"])
        self.assertEqual(result["reviews"][0]["reason"], "unknown_sido_prefix")

    def test_same_destination_is_not_overwritten(self):
        result = analyze_rows([source(), source(rsrcNo="TEST-2")], [lot()])
        self.assertEqual(len(result["matched"]), 1)
        self.assertEqual(len(result["reviews"]), 1)
        self.assertEqual(result["reviews"][0]["reason"], "destination_already_assigned")

    def test_available_address_conflict_stays_in_review(self):
        result = analyze_rows([source(addr="서울특별시 알파구 베타로 20")], [lot(), lot(id="legacy-2", name="베타", addr="서울특별시 알파구 베타로 20", lat=37.6, lng=127.1)])
        self.assertFalse(result["matched"])
        self.assertEqual(result["reviews"][0]["reason"], "available_evidence_conflict")

    def test_geography_accepts_korean_coordinate_and_rejects_outlier(self):
        self.assertTrue(geography_status(source())["ok"])
        self.assertFalse(geography_status(source(lat=10, lot=127))["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
