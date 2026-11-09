# -*- coding: utf-8 -*-
"""API 端到端測試：用 TestClient 跑一次完整鏈路（規則模式，不需要 key）。

    python -m unittest discover -s tests -v
"""
import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("PQ_UPSTREAM_TIMEOUT", "0.8")
os.environ.setdefault("PQ_LEDGER_PATH", "data/test_ledger.db")  # 測試不要污染示範用的歷史記錄
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402


class TestApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 測試必須離線、可重現：強制規則模式，不呼叫真實大模型
        settings.brain_topology = "rule"
        settings.llm_api_key = ""
        cls.client = TestClient(app)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    def test_health(self):
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "ok")

    def test_meta(self):
        r = self.client.get("/api/v1/meta")
        body = r.json()
        self.assertIn("brain", body)
        self.assertIn("prompts", body)

    def test_run_rule_mode(self):
        r = self.client.post("/api/v1/commander/run", json={"question": "生成今天的晨報", "topology": "rule"})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["kind"], "brief")
        self.assertEqual(body["brain"]["mode"], "rule")
        self.assertTrue(body["run_id"].startswith("r-"))
        self.assertIn("guard", body)
        self.assertEqual(body["guard"]["a4_gate"], "advisory_only")

    def test_replay_returns_drift(self):
        run = self.client.post("/api/v1/commander/run", json={"question": "重播用"}).json()
        r = self.client.post("/api/v1/runs/%s/replay" % run["run_id"])
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("drift", body)
        self.assertIn("confidence_delta", body["drift"])

    def test_probe(self):
        r = self.client.post("/api/v1/commander/probe", json={
            "title": "", "text": "央行宣布加息 25 個基點，並表示將繼續收緊。",
            "source": "Reuters", "url": "https://www.reuters.com/x"})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["kind"], "probe")
        self.assertIn(body["direction"], ["偏空", "偏多", "中性"])
        self.assertTrue(body["guard"]["passed"] or body["guard"]["note"])

    def test_probe_rejects_empty(self):
        r = self.client.post("/api/v1/commander/probe", json={"title": "", "text": "   "})
        self.assertEqual(r.status_code, 422)

    def test_brain(self):
        r = self.client.get("/api/v1/brain")
        body = r.json()
        self.assertEqual(len(body["topologies"]), 4)
        self.assertIn("recommendation", body)

    def test_compare_requires_key(self):
        r = self.client.post("/api/v1/brain/compare", json={"topologies": ["lens", "split"]})
        self.assertIn(r.status_code, (409, 200))

    def test_runs_list(self):
        r = self.client.get("/api/v1/runs")
        body = r.json()
        self.assertIn("items", body)
        self.assertIn("stats", body)

    def test_telegram_dry_run(self):
        run = self.client.post("/api/v1/commander/run", json={"question": "推送"}).json()
        r = self.client.post("/api/v1/telegram/render", json={"payload": run})
        self.assertEqual(r.status_code, 200)
        self.assertIn("晨報", r.json()["message"])

    def test_stream_rule_mode(self):
        with self.client.stream("GET", "/api/v1/commander/stream?question=stream&topology=rule") as r:
            self.assertEqual(r.status_code, 200)
            body = "".join(chunk for chunk in r.iter_text())
        self.assertIn('"type": "progress"', body)
        self.assertIn('"type": "result"', body)

    def test_ask_returns_answer(self):
        run = self.client.post("/api/v1/commander/run", json={"question": "追問用"}).json()
        r = self.client.post("/api/v1/commander/ask",
                             json={"run_id": run["run_id"], "question": "為什麼關稅偏空？"})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["answer"])
        self.assertIn(body["mode"], ["llm", "retrieval"])

    def test_ask_unknown_run(self):
        r = self.client.post("/api/v1/commander/ask",
                             json={"run_id": "r-nope", "question": "x"})
        self.assertEqual(r.status_code, 404)

    def test_diff_first_or_previous(self):
        run = self.client.post("/api/v1/commander/run", json={"question": "diff 用"}).json()
        r = self.client.get("/api/v1/commander/diff/%s" % run["run_id"])
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("has_previous", body)
        if body["has_previous"]:
            self.assertIn("confidence", body)

    def test_watchlist_roundtrip(self):
        r = self.client.get("/api/v1/watchlist")
        self.assertEqual(r.status_code, 200)
        original = r.json()["terms"]
        put = self.client.put("/api/v1/watchlist", json={"terms": ["測試詞", "半導體"]})
        self.assertEqual(put.json()["terms"], ["測試詞", "半導體"])
        self.client.post("/api/v1/watchlist/reset")
        self.assertEqual(self.client.get("/api/v1/watchlist").json()["terms"], original)

    def test_online_but_empty_keeps_it_blank(self):
        # A1/A2 在線但沒有資料：不能偷偷用示例，要老實留空
        from unittest.mock import patch
        from app import commander as cm, connectors
        meta = {"ok": True, "status": "connected", "latency_ms": 5, "error": None,
                "count": 0, "empty": True, "sampled": False}
        with patch.object(connectors, "fetch_all", return_value=(([], dict(meta)), ([], dict(meta)))):
            p = cm.run("空資料測試", "rule")
        self.assertFalse(p["data_quality"]["sample_mode"])
        self.assertIn("沒有資料", p["source_note"])
        self.assertEqual(p["evidence"], [])
        self.assertEqual(p["coverage"]["missing"], ["A1", "A2", "A3", "A4"])

    def test_dataset_switches_to_sample_when_offline(self):
        from unittest.mock import patch
        from app import commander as cm, connectors
        meta = {"ok": False, "status": "offline", "latency_ms": 2, "error": "port closed"}
        with patch.object(connectors, "fetch_all", return_value=(([], dict(meta)), ([], dict(meta)))):
            p = cm.run("離線測試", "rule")
        self.assertTrue(p["data_quality"]["sample_mode"])
        self.assertGreater(len(p["evidence"]), 0)
        self.assertIn("示例", p["source_note"])

    def test_coverage_contract(self):
        run = self.client.post("/api/v1/commander/run", json={"question": "coverage"}).json()
        cov = run["coverage"]
        self.assertEqual([a["id"] for a in cov["agents"]], ["A1", "A2", "A3", "A4", "A5"])
        a3 = [a for a in cov["agents"] if a["id"] == "A3"][0]
        self.assertEqual(a3["status"], "missing")
        self.assertFalse(a3["present"])

    def test_integration_endpoint(self):
        r = self.client.get("/api/v1/integration")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(len(body["agents"]), 5)
        self.assertIn("sample_data_policy", body)

    def test_greeting_returns_chat_not_brief(self):
        r = self.client.post("/api/v1/commander/run", json={"question": "hello"})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["kind"], "chat")
        self.assertEqual(body["intent"], "greeting")
        self.assertIn("A5", body["reply"])
        self.assertNotIn("sections", body)          # 不該生成晨報
        self.assertTrue(body["suggestions"])

    def test_help_intent(self):
        body = self.client.post("/api/v1/commander/run", json={"question": "你能做什麼"}).json()
        self.assertEqual(body["kind"], "chat")
        self.assertEqual(body["intent"], "help")

    def test_test_intent_reports_status(self):
        body = self.client.post("/api/v1/commander/run", json={"question": "測試"}).json()
        self.assertEqual(body["intent"], "test")
        self.assertIn("advisory_only", body["reply"])

    def test_real_question_still_makes_brief(self):
        body = self.client.post("/api/v1/commander/run",
                                json={"question": "今天該留意什麼風險？", "topology": "rule"}).json()
        self.assertEqual(body["kind"], "brief")
        self.assertIn("sections", body)

    def test_gate_simulation(self):
        run = self.client.post("/api/v1/commander/run", json={"question": "gate"}).json()
        r = self.client.post("/api/v1/commander/gate", json={"run_id": run["run_id"]})
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertTrue(d["simulated"])
        self.assertEqual(d["gate"], "A4 mock")
        self.assertGreaterEqual(len(d["intents"]), 1)
        for it in d["intents"]:
            self.assertIn(it["verdict"], ["pass", "degrade", "reject"])
            self.assertGreaterEqual(it["size_pct"], 0)
        self.assertLessEqual(d["summary"]["notional_pct"], 30.0)
        # A4 未接入 → 一定是模擬，不能真的下單
        self.assertIn("不產生任何下單指令", d["note"])

    def test_gate_rejects_non_brief(self):
        probe = self.client.post("/api/v1/commander/probe", json={"text": "央行加息"}).json()
        r = self.client.post("/api/v1/commander/gate", json={"run_id": probe["run_id"]})
        self.assertEqual(r.status_code, 422)

    def test_gate_unknown_run(self):
        r = self.client.post("/api/v1/commander/gate", json={"run_id": "r-nope"})
        self.assertEqual(r.status_code, 404)

    def test_schedule_status(self):
        r = self.client.get("/api/v1/schedule")
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertIn("enabled", d)
        self.assertRegex(d["time"], r"^\d{2}:\d{2}$")

    def test_unknown_run(self):
        r = self.client.get("/api/v1/runs/r-nope")
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
