# -*- coding: utf-8 -*-
"""投研输出的合规与证据校验层（可直接嵌入任何 Python 專案，零第三方依賴）。

為什麼要這一層
--------------
一份「看起來很專業」的投研輸出，最容易被挑的三件事：
  1. 用了監管不允許的措辭（保證收益、內幕、穩賺）；
  2. 結論引用了不存在的證據（模型編出來的編號）；
  3. 在沒有風控裁決的情況下給出倉位建議（等於無照薦股）。

這一層做三件事，且只做這三件事：
  - 掃描並改寫違規措辭，而不是整段刪掉（保留信息，去掉承諾）；
  - 逐條驗證引用編號是否真實存在，不存在就標 broken 並把該結論降級為「不可採用」；
  - 附加免責聲明與 advisory_only 標記。

設計約束
--------
- 只用標準庫，複製一個檔案就能用；
- 不改變呼叫方的資料結構，只回傳「清洗後的文本 + 校驗報告」；
- 不做任何網絡請求，不依賴模型，結果可重現（同一輸入永遠同一輸出）。

用法
----
    from compliance import Compliance

    guard = Compliance()
    result = guard.apply(text=報告正文, evidence=證據列表)

    result["text"]          # 可安全展示的正文（已改寫違規措辭）
    result["ok"]            # 是否全部通過
    result["blocked"]       # 被改寫的措辭與替換結果
    result["broken"]        # 引用了但不存在的證據編號
    result["disclaimer"]    # 免責聲明（可直接渲染在頁尾）
    result["advisory_only"] # 恆為 True：沒有 A4 風控裁決就沒有倉位建議

    # 若報告是結構化的（每段帶 evidence_ids），用 check_artifact：
    result = guard.check_artifact(artifact, evidence=artifact["evidence"])

自檢
----
    python compliance.py            # 跑內建樣例，全部通過則 exit 0
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

__all__ = ["Compliance", "Verdict", "evidence_pool", "BANNED_PHRASES", "DISCLAIMER"]


# ---------------------------------------------------------------- 詞表

#: 違規措辭 → 中性替換。原則是「保留判斷、去掉承諾」，
#: 例如「必漲」不是刪掉，而是改成「方向上偏多」——信息還在，責任不在了。
BANNED_PHRASES: dict[str, str] = {
    # 保證收益類（最嚴重：直接構成無照投資顧問）
    "穩賺不賠": "歷史上同類情形多數收漲",
    "保證收益": "歷史區間回報",
    "保本保息": "本金波動需自行承擔",
    "零風險": "風險相對較低",
    "無風險": "風險相對較低",
    "無風險套利": "價差機會（存在執行風險）",
    "穩賺": "勝率較高",
    "包賺": "勝率較高",
    "必漲": "方向上偏多",
    "必跌": "方向上偏空",
    "一定漲": "方向上偏多",
    "一定會": "大概率會",
    "肯定會漲": "方向上偏多",
    "肯定會跌": "方向上偏空",
    "保證盈利": "歷史區間回報",
    "立即買入": "可考慮分批建倉",
    "立即賣出": "可考慮分批減倉",
    "all-in": "集中配置需符合風控上限",
    "肯定漲": "方向上偏多",
    "翻倍": "有較大上行空間",
    "暴漲": "短線走強",
    "必將突破": "有機會突破",
    # 內幕／獨家類（合規紅線）
    "內幕消息": "市場傳聞（未經證實）",
    "內幕": "未公開信息（不予採用）",
    "獨家消息": "多家媒體報導",
    "莊家": "主要資金",
    "跟著莊走": "觀察資金流向",
    # 誘導交易類
    "閉眼買": "可考慮分批建倉",
    "梭哈": "控制單一標的權重",
    "滿倉": "維持較高倉位需符合風控上限",
    "全倉": "維持倉位需符合風控上限",
    "all in": "集中配置需符合風控上限",
    "抄底": "逢低分批",
    "上車": "建立部位",
    # 絕對化用語（廣告法／監管口徑）
    "最強": "相對較強",
    "絕對安全": "相對穩健",
}

#: 免責聲明。可整段渲染，不要改寫成更弱的措辭。
DISCLAIMER = (
    "本內容為研究與資訊整理，不構成任何證券、期貨或其他金融產品的"
    "投資建議、要約或邀約。所有觀點基於公開資料與既定假設，可能隨時變更而不另行通知。"
    "文中提及的標的僅用於說明分析邏輯，不代表推薦。"
    "過往表現不代表未來回報，投資涉及本金損失風險，決策前請自行評估或諮詢持牌專業人士。"
    "本系統在未接入風控裁決（A4）時僅提供建議權（advisory_only），不產生任何下單指令。"
)

#: 引用標記：支援 N1 / P3 / W-TSM / K2 / E12 這幾種常見寫法
_CITATION = re.compile(r"[（(\[]\s*([NPWKE]-?[A-Za-z0-9]{1,12})\s*[）)\]]")
_BARE = re.compile(r"\b([NPWKE]-\d{1,3})\b")

#: 倉位類詞彙：A4 未接入時不應出現明確倉位
_POSITION_QTY = re.compile(r"(倉位|持倉|部位|槓桿)\s*\d{1,3}\s*%")
_POSITION_WORD = re.compile(r"滿倉|重倉|清倉|全倉")


def _mask_position(text: str) -> str:
    """保留「建議倉位」這種動詞，只把具體數字換成裁決提示——不要整句換掉。"""
    text = _POSITION_QTY.sub(r"\1（需經 A4 裁決）", text)
    return _POSITION_WORD.sub("（倉位需經 A4 裁決）", text)


def _has_position(text: str) -> bool:
    return bool(_POSITION_QTY.search(text) or _POSITION_WORD.search(text))


def evidence_pool(node: Any, depth: int = 0) -> list | None:
    """從任意形狀的報告裡找出證據池。

    實際專案裡證據常被包好幾層（artifact → report → evidence），
    所以要遞歸找，而不是假定在頂層——這是接進來最容易踩的坑。
    """
    if depth > 6 or not isinstance(node, Mapping):
        return None
    if isinstance(node.get("evidence"), list) and node["evidence"]:
        return list(node["evidence"])
    for key in ("report", "reference", "artifact", "result", "payload", "data"):
        found = evidence_pool(node.get(key), depth + 1)
        if found:
            return found
    return None


@dataclass
class Verdict:
    """一次校驗的結果。欄位名刻意與 PrismQuant A5 的 payload.guard 對齊。"""

    ok: bool = True          # 清洗後的文本是否可發布
    flagged: bool = False    # 原文是否被改動過（禁語、失效引用、倉位）
    text: str = ""
    blocked: list[dict] = field(default_factory=list)      # 被改寫的措辭
    broken: list[str] = field(default_factory=list)        # 引用了但不存在的證據
    advisory_only: bool = True
    disclaimer: str = DISCLAIMER
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "flagged": self.flagged,
            "text": self.text,
            "blocked": self.blocked,
            "broken": self.broken,
            "advisory_only": self.advisory_only,
            "disclaimer": self.disclaimer,
            "notes": self.notes,
        }


class Compliance:
    """合規與證據校驗器。執行緒安全（無共享可變狀態）。"""

    def __init__(
        self,
        banned: Mapping[str, str] | None = None,
        disclaimer: str = DISCLAIMER,
        a4_connected: bool = False,
        extra_banned: Iterable[str] = (),
    ) -> None:
        self.banned = dict(banned or BANNED_PHRASES)
        for word in extra_banned:
            self.banned.setdefault(word, "（已改寫）")
        self.disclaimer = disclaimer
        self.a4_connected = bool(a4_connected)
        # 長詞優先，避免「穩賺不賠」先被「穩賺」吃掉
        self._pattern = re.compile(
            "|".join(re.escape(w) for w in sorted(self.banned, key=len, reverse=True)),
            re.IGNORECASE,
        )

    # ------------------------------------------------------------ 對外
    def apply(
        self,
        text: str,
        evidence: Sequence[Mapping[str, Any]] | None = None,
        allowed_ids: Iterable[str] | None = None,
        check_position: bool = True,
    ) -> dict:
        """清洗一段文本，並校驗其中引用的證據編號。

        evidence：證據列表，每項至少要有 id（其餘欄位不影響校驗）。
        allowed_ids：若呼叫方的證據集合不是 list[dict]，可直接傳 id 集合。
        """
        verdict = Verdict(text=text or "")
        verdict.text, verdict.blocked = self._scrub(verdict.text)

        known = self._known_ids(evidence, allowed_ids)
        cited = self._citations(verdict.text)
        if known is not None:                       # None = 呼叫方未提供證據集，跳過校驗
            verdict.broken = [c for c in cited if c not in known]
            if verdict.broken:
                verdict.ok = False
                verdict.flagged = True
                verdict.notes.append(
                    "以下引用找不到對應證據，相關結論不可採用：%s" % "、".join(verdict.broken)
                )
                verdict.text = self._mark_unusable(verdict.text, verdict.broken)

        if check_position and not self.a4_connected and _has_position(verdict.text):
            verdict.flagged = True
            verdict.notes.append("A4 風控未接入，不得給出明確倉位；已改為裁決提示")
            verdict.text = _mask_position(verdict.text)

        if verdict.blocked:
            verdict.flagged = True
            verdict.notes.append("已改寫 %d 處違規措辭" % len(verdict.blocked))
        return verdict.as_dict()

    def check_artifact(
        self,
        artifact: Mapping[str, Any],
        evidence: Sequence[Mapping[str, Any]] | None = None,
        text_keys: Sequence[str] = ("summary", "body", "text", "answer", "content"),
    ) -> dict:
        """校驗結構化報告：把每段的正文清洗，並收集各段的 evidence_ids。

        artifact 的常見形狀（Winter / PrismQuant 都適用）：
            {"summary": "...", "sections": [{"analysis": "...", "evidence_ids": ["N1"]}],
             "evidence": [{"id": "N1", ...}]}
        這裡不假定固定 schema，只遞歸找字串與 evidence_ids。
        """
        pool = list(evidence or evidence_pool(artifact) or [])
        known = self._known_ids(pool, None)
        texts: list[str] = []
        cited: list[str] = []
        self._walk(artifact, texts, cited, text_keys)

        body = "\n\n".join(t for t in texts if t.strip())
        verdict = Verdict(text=body)
        verdict.text, verdict.blocked = self._scrub(body)

        if known is not None:
            verdict.broken = sorted({c for c in cited if c not in known})
            if verdict.broken:
                verdict.ok = False
                verdict.flagged = True
                verdict.notes.append(
                    "以下引用找不到對應證據，相關結論不可採用：%s" % "、".join(verdict.broken)
                )
                verdict.text = self._mark_unusable(verdict.text, verdict.broken)

        if not self.a4_connected and _has_position(verdict.text):
            verdict.flagged = True
            verdict.notes.append("A4 風控未接入，不得給出明確倉位；已改為裁決提示")
            verdict.text = _mask_position(verdict.text)

        if verdict.blocked:
            verdict.flagged = True
            verdict.notes.append("已改寫 %d 處違規措辭" % len(verdict.blocked))
        return verdict.as_dict()

    # ------------------------------------------------------------ 內部
    def _scrub(self, text: str) -> tuple[str, list[dict]]:
        hits: list[dict] = []

        def sub(m: re.Match) -> str:
            word = m.group(0)
            replacement = self.banned.get(word) or self.banned.get(word.lower(), "（已改寫）")
            hits.append({"phrase": word, "replacement": replacement})
            return replacement

        return self._pattern.sub(sub, text or ""), hits

    @staticmethod
    def _known_ids(
        evidence: Sequence[Mapping[str, Any]] | None,
        allowed: Iterable[str] | None,
    ) -> set[str] | None:
        if allowed is not None:
            return {str(x) for x in allowed}
        if evidence is None:
            return None                    # 呼叫方沒提供 → 不校驗，而不是全部判 broken
        ids = set()
        for item in evidence:
            if isinstance(item, Mapping):
                for key in ("id", "evidence_id", "ref", "symbol_id"):
                    if item.get(key):
                        ids.add(str(item[key]).strip())
                        break
        return ids

    @staticmethod
    def _citations(text: str) -> list[str]:
        found = [m.group(1) for m in _CITATION.finditer(text or "")]
        found += [m.group(1) for m in _BARE.finditer(text or "")]
        seen, out = set(), []
        for c in found:
            key = c.upper()
            if key not in seen:
                seen.add(key)
                out.append(key)
        return out

    @staticmethod
    def _mark_unusable(text: str, broken: Sequence[str]) -> str:
        """在句子層面標記：含失效引用的那一句加註，而不是把整段刪掉。"""
        if not broken:
            return text
        pattern = re.compile(
            "|".join(re.escape(b) for b in broken), re.IGNORECASE
        )
        lines = []
        for line in (text or "").splitlines():
            if pattern.search(line):
                line = line.rstrip() + "　⚠ 引用失效，本句不可採用"
            lines.append(line)
        return "\n".join(lines)

    def _walk(self, node: Any, texts: list[str], cited: list[str], keys: Sequence[str]) -> None:
        if isinstance(node, Mapping):
            for key, value in node.items():
                if key == "evidence":
                    continue                       # 證據池本身不當正文
                if key == "evidence_ids" and isinstance(value, (list, tuple)):
                    cited.extend(str(v).strip().upper() for v in value)
                    continue
                if isinstance(value, str) and key in keys:
                    texts.append(value)
                    cited.extend(self._citations(value))
                else:
                    self._walk(value, texts, cited, keys)
        elif isinstance(node, (list, tuple)):
            for item in node:
                self._walk(item, texts, cited, keys)


# ---------------------------------------------------------------- 自檢

def _selftest() -> int:
    guard = Compliance()
    cases: list[tuple[str, str, bool]] = []

    # 1) 禁語改寫
    r = guard.apply("這隻股票必漲，穩賺不賠，可以梭哈。")
    cases.append(("禁語被改寫", r["text"], "必漲" not in r["text"] and "穩賺" not in r["text"]))
    cases.append(("改寫有記錄", str(len(r["blocked"])), bool(r["blocked"]) and r["flagged"] and r["ok"]))

    # 2) 引用存在
    ev = [{"id": "N1"}, {"id": "P2"}]
    r = guard.apply("關稅偏空（N1），政策面亦偏空（P2）。", evidence=ev)
    cases.append(("引用全部存在", str(r["broken"]), not r["broken"]))

    # 3) 引用不存在
    r = guard.apply("半導體需求強勁（N9）。", evidence=ev)
    cases.append(("失效引用被抓", r["broken"], r["broken"] == ["N9"] and not r["ok"]))
    cases.append(("失效句被標記", r["text"], "不可採用" in r["text"]))

    # 4) 未提供證據集時不誤報
    r = guard.apply("隨便一句（N99）。")
    cases.append(("無證據集不誤判", str(r["broken"]), not r["broken"]))

    # 5) A4 未接入不得給倉位
    r = guard.apply("建議倉位 30%。", evidence=ev)
    cases.append(("倉位被攔", r["text"], "建議倉位（需經 A4 裁決）" in r["text"] and r["flagged"]))

    # 6) 結構化報告
    art = {
        "summary": "整體偏空，保證收益。",
        "sections": [{"analysis": "關稅壓力（N1）", "evidence_ids": ["N1", "N8"]}],
        "evidence": ev,
    }
    r = guard.check_artifact(art)
    cases.append(("結構化：抓到失效", str(r["broken"]), r["broken"] == ["N8"]))
    cases.append(("結構化：禁語改寫", r["text"][:20], "保證收益" not in r["text"]))

    # 7) 免責聲明存在且不可為空
    cases.append(("免責聲明", r["disclaimer"][:12], len(r["disclaimer"]) > 60))

    failed = 0
    for name, detail, ok in cases:
        print(("PASS  " if ok else "FAIL  ") + name + "　" + str(detail)[:60])
        failed += 0 if ok else 1
    print("\n%d / %d 通過" % (len(cases) - failed, len(cases)))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(_selftest())
