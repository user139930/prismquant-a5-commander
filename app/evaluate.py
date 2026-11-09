# -*- coding: utf-8 -*-
"""離線評測（回歸測試的另一半）。

    python -m app.evaluate

為什麼要有這個：
隊友說「沒有提示詞訓練過，之後要加」。在加訓練之前，先要有**可重複的驗收標準**，
否則改了 prompt 或加了新主題，沒人知道是變好還是變壞。這個腳本把「好」寫成硬條件：

    - 主線句不能空、章節不能少於三條
    - 每份晨報至少兩個可追溯證據 ID
    - 方向與置信度必須落在合法區間
    - 禁語一個都不能漏
    - 閱讀時間不能超過設定上限
    - 上游全空時仍然要產出「沒有消息」而不是報錯

全部通過才 exit 0，可以直接掛進 CI。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import analyze, connectors, guardrails, probe  # noqa: E402
from app.config import settings  # noqa: E402

CHECKS = []


def check(name: str, ok: bool, detail: str = ""):
    CHECKS.append({"name": name, "ok": bool(ok), "detail": detail})
    return ok


def _bundle():
    b = connectors.sample_bundle()
    return b["a1"]["events"], b["a2"]["events"]


def run_brief_case(name: str, a1, a2, question="評測"):
    payload = analyze.build_brief(question, a1, a2, True, settings.watchlist, {})
    clean = guardrails.apply(payload)

    prefix = "[%s] " % name
    check(prefix + "主線非空", bool(clean.get("headline")), clean.get("headline", ""))
    sec = clean.get("sections") or {}
    check(prefix + "章節齊全",
          len(sec.get("what_happened", [])) >= 1 and len(sec.get("so_what", [])) >= 1 and bool(sec.get("watch")))
    text = " ".join(sec.get("what_happened", []) + sec.get("so_what", []))
    import re
    cites = len(re.findall(r"\b[NP]\d{1,2}\b", text))
    check(prefix + "證據引用 >= 2", cites >= 2 or not a1 and not a2)
    check(prefix + "置信度區間", 0.15 <= clean["confidence"]["score"] <= 0.85,
          "%.2f" % clean["confidence"]["score"])
    check(prefix + "方向合法", all(t["direction"] in {"偏多", "偏空", "中性"} for t in clean["transmission"]))
    check(prefix + "證據可追溯", not clean["guard"]["broken_evidence"],
          ",".join(clean["guard"]["broken_evidence"]))
    check(prefix + "未觸發禁語", not clean["guard"]["blocked"],
          ",".join(b["phrase"] for b in clean["guard"]["blocked"]))
    check(prefix + "閱讀時間", clean["reading_minutes"] <= settings.brief_target_max_minutes,
          "%.1f 分鐘" % clean["reading_minutes"])
    return clean


def main() -> int:
    a1, a2 = _bundle()

    run_brief_case("基準", a1, a2)

    # A2 離線：政策維度缺失，置信度必須被扣分
    p = run_brief_case("A2離線", a1, [])
    check("[A2離線] 置信度被扣", p["confidence"]["score"] <= 0.6, "%.2f" % p["confidence"]["score"])

    # 只有一條新聞 + 同一家媒體：必須標成單一來源
    p = run_brief_case("單一來源", a1[:1], [])
    check("[單一來源] 標記單源", bool(p["data_quality"]["single_source_topics"]),
          ",".join(p["data_quality"]["single_source_topics"]))
    check("[單一來源] 置信度偏低", p["confidence"]["score"] < 0.5, "%.2f" % p["confidence"]["score"])

    # 上游全空：不能炸，要老實說沒消息
    p = run_brief_case("空上游", [], [])
    check("[空上游] 明確講無消息", "沒有" in "".join(p["sections"]["what_happened"]))

    # 禁語注入：必須被刪掉
    dirty = dict(p)
    dirty["headline"] = "這隻股票必漲，穩賺不賠"
    cleaned = guardrails.apply(dirty)
    check("[禁語] 攔截成功", not cleaned["guard"]["passed"] and "必漲" not in cleaned["headline"])

    # 探針
    r = guardrails.apply(probe.analyze("", "央行宣布加息 25 個基點，並表示將繼續收緊。", "Reuters", ""))
    check("[探針] 方向偏空", r["direction"] == "偏空", r["direction"])
    check("[探針] 有傳導鏈", len(r["chain"]) == 4)
    check("[探針] 有作廢條件", bool(r["what_would_change"]))

    r2 = guardrails.apply(probe.analyze("", "某公司公布季度財報，營收同比增長 12%。", "財新", ""))
    check("[探針] 主題=企業業績", r2["topic"] == "企業業績", r2["topic"])

    # 輸出
    width = max(len(c["name"]) for c in CHECKS) + 2
    print("PrismQuant A5 · 離線評測（提示詞版本 %s）" % settings.prompt_variant)
    print("-" * (width + 46))
    for c in CHECKS:
        flag = "PASS" if c["ok"] else "FAIL"
        print("%-6s %-*s %s" % (flag, width, c["name"], c["detail"]))
    print("-" * (width + 46))
    passed = sum(1 for c in CHECKS if c["ok"])
    print("%d / %d 通過" % (passed, len(CHECKS)))
    return 0 if passed == len(CHECKS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
