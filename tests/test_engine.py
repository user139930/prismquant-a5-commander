# -*- coding: utf-8 -*-
"""規則層單元測試：不依賴網絡、不依賴大模型。

    python -m unittest discover -s tests -v
"""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import analyze, connectors, guardrails, probe  # noqa: E402


class TestClassify(unittest.TestCase):
    def test_tariff_keyword(self):
        key, hits = analyze.classify("美國擬對進口半導體設備加徵 25% 關稅")
        self.assertEqual(key, "tariff")
        self.assertIn("tariff", hits)

    def test_crypto_beats_regulation(self):
        key, _ = analyze.classify("監管機構就穩定幣儲備披露規則徵求意見")
        self.assertEqual(key, "crypto")

    def test_unknown_falls_back_to_macro(self):
        key, hits = analyze.classify("今天天氣很好")
        self.assertEqual(key, "macro")
        self.assertEqual(hits, [])


class TestPublisher(unittest.TestCase):
    def test_generic_rss_resolved_from_url(self):
        self.assertEqual(
            analyze.publisher_of("RSS", "https://www.bbc.com/news/business-123"), "BBC")
        self.assertEqual(
            analyze.publisher_of("RSS", "https://www.reuters.com/markets/x"), "Reuters")

    def test_explicit_source_wins(self):
        self.assertEqual(analyze.publisher_of("財新", ""), "財新")

    def test_unknown_host(self):
        self.assertEqual(analyze.publisher_of("RSS", "https://news.example.org/a"), "news.example.org")


class TestBrief(unittest.TestCase):
    def setUp(self):
        bundle = connectors.sample_bundle()
        self.a1 = bundle["a1"]["events"]
        self.a2 = bundle["a2"]["events"]
        self.payload = analyze.build_brief("測試", self.a1, self.a2, True, ["關稅"], {})

    def test_sections_present(self):
        p = self.payload
        self.assertTrue(p["headline"])
        self.assertGreaterEqual(len(p["sections"]["what_happened"]), 3)
        self.assertGreaterEqual(len(p["sections"]["so_what"]), 3)
        self.assertTrue(p["sections"]["watch"])

    def test_every_citation_resolves(self):
        ids = {e["id"] for e in self.payload["evidence"]}
        for row in self.payload["transmission"]:
            for ref in row["evidence"]:
                self.assertIn(ref, ids)

    def test_confidence_in_range(self):
        score = self.payload["confidence"]["score"]
        self.assertGreaterEqual(score, 0.15)
        self.assertLessEqual(score, 0.85)
        self.assertLessEqual(score, 0.55, "示例資料必須被壓在 0.55 以下")
        self.assertTrue(self.payload["confidence"]["reasons"])

    def test_conflict_detected(self):
        topics = [c["topic"] for c in self.payload["conflicts"]]
        self.assertIn("能源與商品", topics)
        for c in self.payload["conflicts"]:
            self.assertTrue(c["breaks_if"])

    def test_single_source_flagged(self):
        self.assertTrue(self.payload["data_quality"]["single_source_topics"])

    def test_reading_time(self):
        self.assertGreater(self.payload["reading_minutes"], 0)
        self.assertLess(self.payload["reading_minutes"], 15)


class TestGuardrails(unittest.TestCase):
    def test_banned_phrase_removed(self):
        payload = {"headline": "這隻股票必漲，穩賺不賠。", "sections": {"what_happened": ["正常內容"]}}
        clean = guardrails.apply(payload)
        # 對外文字（headline / 章節）必須乾淨；guard 報告本身會記錄原詞，這是審計需要
        self.assertNotIn("必漲", clean["headline"])
        self.assertNotIn("穩賺", clean["headline"])
        self.assertIn(guardrails.PLACEHOLDER, clean["headline"])
        self.assertEqual(clean["sections"]["what_happened"], ["正常內容"])
        self.assertFalse(clean["guard"]["passed"])
        self.assertEqual(clean["guard"]["a4_gate"], "advisory_only")

    def test_broken_evidence_detected(self):
        payload = {"evidence": [{"id": "N1"}],
                   "transmission": [{"evidence": ["N1", "P9"]}],
                   "conflicts": []}
        clean = guardrails.apply(payload)
        self.assertIn("P9", clean["guard"]["broken_evidence"])
        self.assertFalse(clean["guard"]["passed"])

    def test_clean_payload_passes(self):
        clean = guardrails.apply({"headline": "中性偏空", "evidence": [], "transmission": [], "conflicts": []})
        self.assertTrue(clean["guard"]["passed"])
        self.assertIn("不構成投資建議", clean["disclaimer"])


class TestProbe(unittest.TestCase):
    def test_probe_single_source_is_logged(self):
        r = probe.analyze("", "某公司宣布裁員 200 人。", "Reuters", "")
        self.assertEqual(r["kind"], "probe")
        self.assertEqual(r["verdict_level"], "log")
        self.assertEqual(r["confidence"]["level"], "低")

    def test_probe_detects_duplicate(self):
        existing = analyze.normalize_a1(connectors.sample_bundle()["a1"]["events"])
        analyze.assign_ids(existing)
        r = probe.analyze("", "美國擬擴大先進芯片設備出口限制，涉及 12 家供應商", "Bloomberg", "", existing)
        self.assertTrue(r["corroboration"]["matched"])
        self.assertEqual(r["topic"], "關稅與貿易")


class TestScheduler(unittest.TestCase):
    def test_next_run_is_future_and_same_clock(self):
        from app import scheduler
        import datetime as dt
        now = dt.datetime(2026, 11, 10, 9, 0, tzinfo=scheduler.HK)
        nxt = scheduler.next_run_at(now)
        self.assertGreater(nxt, now)
        self.assertEqual((nxt.hour, nxt.minute), (8, 30))
        self.assertEqual(nxt.date(), dt.date(2026, 11, 11))

    def test_next_run_today_when_before(self):
        from app import scheduler
        import datetime as dt
        now = dt.datetime(2026, 11, 10, 6, 0, tzinfo=scheduler.HK)
        nxt = scheduler.next_run_at(now)
        self.assertEqual(nxt.date(), dt.date(2026, 11, 10))


if __name__ == "__main__":
    unittest.main(verbosity=2)
