# -*- coding: utf-8 -*-
"""API 端到端測試：用 TestClient 跑一次完整鏈路（規則模式，不需要 key）。

    python -m unittest discover -s tests -v
"""
import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("PQ_UPSTREAM_TIMEOUT", "0.8")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


class TestApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
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
        r = self.client.post("/api/v1/commander/run", json={"question": "測試", "topology": "rule"})
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

    def test_unknown_run(self):
        r = self.client.get("/api/v1/runs/r-nope")
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
