# 合規檢查 API · 給 Winter 的接入說明

一個端點，一次 HTTP 調用。不需要合併代碼，也不用改你的資料結構。

```
POST http://127.0.0.1:8130/api/v1/guard
```

## 為什麼是 API 而不是合併代碼

你的 A5 是調度器（派工給 A1 / A3），我的 A5 是分析與裁決。兩邊的資料模型不同
（你是 job/artifact，我是 payload/evidence），硬塞只會互相變形。

服務調服務最省事：你在寫入訊息前 POST 一次，拿到乾淨的文本繼續，出問題就跳過。

## 請求

```json
{
  "text": "要檢查的正文",
  "evidence_ids": ["N1", "P2"],
  "a4_connected": false
}
```

| 欄位 | 說明 |
|---|---|
| `text` | 要清洗的文字。可以是整段報告、單條訊息、或標題 |
| `evidence_ids` | 你手上現有的證據編號。**留空或省略 = 跳過引用校驗**，只做措辭改寫 |
| `a4_connected` | A4 風控接上後傳 `true`；`false` 時不允許出現明確倉位 |

## 回傳

```json
{
  "text": "清洗後的正文",
  "passed": false,
  "flagged": true,
  "blocked": [{"phrase": "必漲", "replacement": "方向上偏多"}],
  "broken": ["N9"],
  "notes": ["已改寫 2 處違規措辭"],
  "disclaimer": "本內容為研究與資訊整理……",
  "advisory_only": true
}
```

| 欄位 | 怎麼用 |
|---|---|
| `text` | **直接用這個取代原文**，其餘欄位不用改你的流程 |
| `passed` | `false` 表示有東西被改過或引用對不上，可以據此決定要不要顯示提示 |
| `blocked` | 改了什麼、改成什麼。做審計或前端小標籤用 |
| `broken` | 對不上的引用編號，相關句子在 `text` 裡已標「⚠ 引用失效，本句不可採用」 |
| `disclaimer` | 免責聲明，建議渲染在頁尾 |
| `advisory_only` | 恆為 `true`（A4 未接），表示沒有下單指令 |

## 接法（Python，接在 `_message` 之前）

```python
import json
from urllib.request import Request, urlopen

A5_GUARD = "http://127.0.0.1:8130/api/v1/guard"

def guard(text: str, evidence_ids=None) -> str:
    """調 A5 的合規層。任何失敗都回原文——不能因為 A5 掛了就發不出去。"""
    try:
        body = json.dumps({
            "text": text,
            "evidence_ids": evidence_ids or [],
            "a4_connected": False,
        }).encode()
        req = Request(A5_GUARD, data=body,
                      headers={"Content-Type": "application/json"})
        with urlopen(req, timeout=1.5) as resp:
            return json.loads(resp.read())["text"]
    except Exception:
        return text          # 降級：原文照發
```

然後在 `Coordinator._message` 裡：

```python
@staticmethod
def _message(db, conversation_id, job_id, role, agent, content, artifact=None):
    content = guard(content)        # ← 只加這一行
    db.execute(...)
```

## 三個要注意的

**1. 一定要有超時和降級。** 範例裡是 1.5 秒。合規層是加分項，不該成為單點故障。

**2. 不要對同一段文字重複調用。** 它是冪等的（改過的文本再送一次不會再變），但沒必要浪費。

**3. 證據編號要跟你自己的對得上。** 如果你的編號是 `EV-001` 這種格式，直接傳進來就行，
校驗只看「這個編號在不在你給的清單裡」，不關心格式。

## 自己驗一下

```bash
curl -s -X POST http://127.0.0.1:8130/api/v1/guard \
  -H "Content-Type: application/json" \
  -d '{"text":"這隻股票必漲，穩賺不賠，建議倉位 30%。","evidence_ids":[]}'
```

應該回：

```json
{"text":"這隻股票方向上偏多，歷史上同類情形多數收漲，建議倉位（需經 A4 裁決）。",
 "passed":false,"flagged":true,
 "blocked":[{"phrase":"必漲","replacement":"方向上偏多"},
            {"phrase":"穩賺不賠","replacement":"歷史上同類情形多數收漲"}],
 "broken":[],"advisory_only":true}
```

---

詞表和改寫規則在 `app/compliance.py`，只有一個 dict，要加詞直接改那裡。
有問題群裡問。
