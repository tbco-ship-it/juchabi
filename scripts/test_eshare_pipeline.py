#!/usr/bin/env python3
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from normalize_eshare import (  # noqa: E402
    analyze_rows,
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
