# 架構與實作決策

## 1. 一次 run 的順序

```
使用者提問
   │
   ├─(1) 取上游 ── A1 /api/v1/news/events ──┐
   │                A2 /api/v1/analyses ─────┤ 並行（ThreadPool），離線時總延遲由慢的一邊決定
   │                                          └─ 任一失敗 → 整份改用示例資料（不真假混用）
   │
   ├─(2) 規則層 ── L0 正規化 → L1 交叉驗證 → L2 情境推演   ← 事實只由這一層產生
   │
   ├─(3) 大腦層 ── rule / shared / lens / split            ← 只負責「表達」，不准改事實
   │                失敗或輸出不合契約 → 自動降級回規則層（不靜默失敗）
   │
   ├─(4) 護欄 ── 禁語掃描 → 證據可追溯 → A4 閘門（advisory_only）
   │
   └─(5) 留檔 ── Run Ledger：payload + 輸入快照 + 拓撲 + 提示詞版本
```

**關鍵設計：事實與表達分離。**
大模型拿到的是規則層算好的 clusters / evidence / conflicts / confidence，它只能改寫文字，
不能改數字、不能新增事件。輸出要通過契約驗證（headline 非空、章節 ≥2、brief ≥80 字），
否則整份丟棄並降級。這樣「模型幻覺」不會變成「假事實」，最多是「表達不好」。

---

## 2. 資料契約

### A1 輸入

```json
{"agent":"A1 新聞情報","count":8,"sampled":false,
 "events":[{"title":"…","summary":"…","sentiment":"利多|利空|中性",
            "topics":["關稅"],"instruments":["SOX"],"confidence":0.76,
            "source":"RSS","url":"https://…","published":"RFC822 或 ISO"}]}
```

`source` 只給 `"RSS"` 時，A5 從 `url` 的 host 反推媒體名（`bbc.com → BBC`）。
這是刻意的補洞：A1 目前只抓 BBC 兩條 feed，介面上必須看得出「只有一個媒體」，
否則「來源太少」這個問題永遠不會被正視。

### A2 輸入（主路徑）

```json
{"agent":"A2 政要監控","events":[{"title":"…","summary":"…","direction":"利空",
  "entities":["USTR"],"markets":["美股"],"confidence":0.7}]}
```

### A2 輸入（退化路徑）

A2 若只開放 `/api/v1/public-records`（無方向、無實體），A5 會：
1. 把 `body_text` 截 200 字當 summary；
2. 方向一律「中性」；
3. 在 `upstream.a2.degraded = true` 標記，並在資料充分性面板提示。

目的是：A2 由 RSS 換成爬蟲、schema 微調時，A5 不會整條鏈斷掉。

### A5 輸出（摘要）

```json
{"run_id":"r-20261110-0117-ab12","kind":"brief","headline":"…",
 "sections":{"what_happened":["…（證據 N1、N3）"],"so_what":["…"],"watch":[{...}]},
 "transmission":[{"from":"關稅與貿易","channel":"成本與供應鏈","to":["SOX"],
                  "direction":"偏空","strength":"中","strength_value":0.49,
                  "horizon":"1–4 週","evidence":["N2","N3"]}],
 "conflicts":[{"topic":"能源與商品","a1":"…","a2":"…","resolution":"…","breaks_if":"…"}],
 "scenarios":[{"name":"基準情境","prob":0.41,"path":"…","triggers":["…"]}],
 "evidence":[{"id":"N1","agent":"A1","publisher":"Reuters","age_hours":2.4,...}],
 "confidence":{"score":0.54,"level":"中","reasons":["+0.12 …","-0.08 …"]},
 "data_quality":{"publishers":[{"name":"Reuters","count":3}],"single_source_topics":["…"]},
 "guard":{"passed":true,"blocked":[],"a4_gate":"advisory_only"},
 "brain":{"topology":"lens","mode":"rule","prompt_variant":"v1"}}
```

---

## 3. 降級矩陣

| 失敗 | 行為 | 使用者看到 |
|---|---|---|
| A1 離線 | 整份示例資料 | 頂部紅字：「上游 A1 未連線，整份改用示例資料」 |
| A2 離線 | 整份示例資料 | 同上 |
| A2 只有原始記錄 | 用退化 schema，標 `degraded` | 黃字提示「A2 未提供影響分析」 |
| 無 LLM key | 規則層 | 黃字「已自動降級為規則層（不影響其餘功能）」 |
| LLM 超時 / JSON 不合契約 | 丟棄模型輸出，用規則層 | 黃字帶錯誤摘要 |
| 置信度過低 | 主線句改成「資訊不足，本期只做風險提示」 | 置信度低（紅圈） |
| 上游全空 | 仍然出一份「沒有消息」，watch 指向採集健康 | 不會白屏 |

**唯一的硬失敗**：`/api/v1/commander/probe` 傳空輸入 → 422。這是使用者錯誤，不是系統降級。

---

## 4. 合規護欄

三層，依序執行：

1. **禁語掃描**：按句處理，命中即整句替換為「已按合規規則刪除不當表述」，並記錄在
   `guard.blocked`（含路徑與詞）。禁語清單在 `PQ_BANNED_PHRASES`，可配置。
2. **證據可追溯**：`transmission` 與 `conflicts` 引用的每個 ID 必須存在於 `evidence`。
   缺一個就把 `passed=false` 並列出，前端顯示「相關結論請勿採用」。
3. **執行邊界**：`PQ_A4_API_URL` 為空 → `a4_gate=advisory_only`，輸出附帶
   「A5 只有建議權，不構成下單指令」。

另外每次輸出都附 `disclaimer`（不代客理財、不託管、非投資建議、政治表態為概率標籤）。

---

## 5. 提示詞版本化與評測

```
app/prompts/
  v1/brief_system.md
  v1/brief_user.md
```

`PQ_PROMPT_VARIANT` 決定載入哪個資料夾。新增 `v2/` 不用改任何程式碼；
`/api/v1/meta` 會列出所有版本與目前啟用者，`Run Ledger` 也記錄每次 run 用的版本。
這樣「改了 prompt 導致輸出變差」可以回頭定位。

`python -m app.evaluate` 是回歸門檻（41 項）：
章節齊全、證據 ≥2、方向合法、無禁語、閱讀時間 ≤ 上限、A2 離線要扣分、單源要降級、空上游不能炸。

---

## 6. 幾個不明顯但重要的決定

| 決定 | 原因 |
|---|---|
| 上游 `httpx(trust_env=False)` | 本機服務不能被 `HTTP_PROXY` 帶走，否則開著代理的機器每次 run 多 4–6 秒 |
| A1/A2 並行取數 | 離線時總延遲由慢的一邊決定，而不是相加 |
| A2 連線層失敗就跳過 fallback | 兩個端點在同一個 host，連不上就都連不上，不必再付一次 timeout |
| 整份用示例，而不是逐條混用 | 混用比全假更危險，讀者分不清哪句是真的 |
| 置信度以規則加減分，不讓模型自評 | 模型自評沒有校準，且無法重現；規則分數可以被追問 |
| 閱讀時間按字數算，不寫死「10 分鐘」 | 誠實的 2.2 分鐘比虛報的 10 分鐘有說服力 |
| 分歧一定要寫 `breaks_if` | 沒有作廢條件的判斷等於不可否證，不是分析 |
| Run Ledger 存輸入快照 | replay 要能重跑同一份輸入，才能量測「漂移」而不是「重播畫面」 |

---

# v0.3 擴充：接上真實大模型之後

## 7. 大模型只寫「主線句 + 正文」

第一次真跑（讓模型寫全部章節）得到兩個數字：**65 秒、約 1,100 輸出 tokens**。
DeepSeek 經 OpenRouter 實測只有 15–20 tok/s，這條路在示範現場不可用。

於是輸出契約縮到兩個欄位：

```json
{"headline": "主線＋限制（20 字內）", "brief": "300–420 字連續正文"}
```

`what_happened / so_what / watch` 一律由規則層產出。這不只是為了快：

- **結構不會壞**：模型抽風、截斷、JSON 壞掉，只影響一句話和一段文，章節仍然完整。
- **事實不會漂**：章節裡的數字與 ID 由程式產生，模型沒有機會改。
- **延遲可預測**：約 20 秒，與輸入長度關係不大。

驗證方式：`_validate_llm()` 只檢查 headline 長度（6–60 字）與 brief 長度（≥150 字），
不合格就整份丟棄並降級，`brain.degraded` 會記錄原因。

## 8. 進度串流（SSE）

`GET /api/v1/commander/stream` 用 `StreamingResponse` 推 `text/event-stream`，
每次 `commander.run(progress=...)` 回呼就送一個事件：

```
data: {"type":"progress","stage":"upstream","message":"呼叫 A1…","elapsed_ms":0}
data: {"type":"progress","stage":"analyze","done":true,…}
data: {"type":"progress","stage":"llm","message":"改寫中，約 30–60 秒"}
data: {"type":"result","payload":{…完整晨報…}}
```

前端用 `fetch` + `ReadableStream` 逐段解析（EventSource 只支援 GET 且不能自訂 body，這裡夠用）。
為什麼不用背景任務 + 輪詢：一次請求就是一次 run，沒有中間狀態要持久化，串流最簡單。

## 9. 追問（多輪）

`POST /api/v1/commander/ask {run_id, question}`：

1. 從 ledger 取回那份晨報的完整 payload；
2. 把 headline、clusters、evidence、conflicts、confidence 壓成事實包；
3. 系統提示要求「只能用這些事實、每條結論帶 ID、300 字內、不准給保證性表述」；
4. 回傳 `answer / cited / limits`，並以 `kind="answer"` 寫入 ledger（所以追問也可審計）。

沒有 key 時退化為 `_ask_retrieval()`：按關鍵詞在證據裡找最相關的四條，明確標示
「這是檢索，不是推理」。寧可承認不會，也不要編。

## 10. 與上一份的差異

`GET /api/v1/commander/diff/{run_id}` 在 ledger 裡往前找最近一份 `kind="brief"`，比對：

- 主題新增／掉出（cluster label 集合差）
- 方向翻轉（同一主題 direction 改變）
- 置信度變化（±0.03 以上才報）
- 新增／消失的證據 ID

這是「每天看晨報」的意義所在：沒有變化時它會直接說「今天不需要改倉位」。

## 11. 關注詞（使用者指令）

`data/prefs.json` 存使用者的關注詞，`PUT /api/v1/watchlist` 改寫。
`commander.run()` 每次讀 `store.get_watchlist()`，命中的詞會進入「要盯什麼」並影響重排。
壞檔或空值時回退到 `PQ_WATCHLIST` 的預設值——使用者的設定不能讓系統起不來。

## 12. 成本

`payload["usage"]` 帶 `prompt_tokens / completion_tokens / cost_usd`，
單價由 `PQ_PRICE_IN_PER_M` / `PQ_PRICE_OUT_PER_M` 決定（預設是 deepseek-chat 的量級）。
實測一份晨報約 3,100 tokens、US$0.0011。200 個訂閱者每日一份約 US$0.22。
這個數字寫進報告比「我們會控制成本」有用。
