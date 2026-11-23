# 接上 A5 的合規層 · 兩行改動

目標：Winter 寫進資料庫之前，讓 A5 過一遍文字。
**不改你的任何邏輯**，只在一處加一行。想回退就刪掉那行。

---

## 第 1 步：放一個檔案

把同目錄的 `a5_guard.py` 複製到：

```
winter_agents/a5_guard.py
```

（不改任何東西，只是多一個檔案）

## 第 2 步：改兩處

### 2a. 檔案頂部，加一個 import

`winter_agents/coordinator.py` 大概第 20 行，和其他 `from .xxx import` 放一起：

```python
from .a5_guard import guard_text
```

### 2b. `_message` 的第一行

找到這個函式（大約 622 行）：

```python
    @staticmethod
    def _message(db, conversation_id, job_id, role, agent, content, artifact=None):
        db.execute(
```

改成：

```python
    @staticmethod
    def _message(db, conversation_id, job_id, role, agent, content, artifact=None):
        content = guard_text(content)                    # ← 只加這一行
        db.execute(
```

就這兩處。其他地方一個字都不用動。

## 第 3 步：驗證

**先開 A5**：雙擊 `a5-commander/start.cmd`，等瀏覽器打開 8130 那個頁面。

然後在專案根目錄執行：

```bash
python -c "from winter_agents.a5_guard import guard_text; print(guard_text('這隻股票必漲，穩賺不賠'))"
```

期望輸出：

```
這隻股票方向上偏多，歷史上同類情形多數收漲
```

如果是原樣輸出（沒被改），表示 A5 沒開或連不上——看下面的排查。

## 第 4 步：看效果

正常用 Winter 生成一次結果。如果報告裡原本有「必漲」「穩賺不賠」這類詞，
現在會被改寫成中性說法。日誌裡會多一行：

```
A5 合規層：改寫 2 處措辭、0 個引用失效
```

---

## A5 沒開會怎樣

**什麼都不會發生。** `guard_text` 連不上就回原文，Winter 照常跑，
只是少了那一層檢查。日誌裡是 `debug` 級別，默認看不到。

三種情況都安全：

| 情況 | 結果 |
|---|---|
| A5 沒啟動 | 原文放行 |
| A5 卡住超過 1.5 秒 | 原文放行 |
| A5 回傳格式不對 | 原文放行 |

## 想徹底回退

刪掉 `content = guard_text(content)` 這一行，其他都不用管。

## 幾個參數

| 環境變數 | 預設 | 說明 |
|---|---|---|
| `A5_GUARD_URL` | `http://127.0.0.1:8130/api/v1/guard` | A5 換端口時改這裡 |
| `A5_GUARD_TIMEOUT` | `1.5` | 逾時秒數，機器慢可以調到 3 |

## 常見問題

**Q：一定要同時開兩個服務嗎？**
只有想看「報告被改寫」這個效果時才需要開 A5。平時跑 Winter 不用開。

**Q：會不會拖慢生成速度？**
每次寫訊息多一次 1.5 秒上限的本機請求。實測 5–20ms。真的卡住也只等 1.5 秒然後放行。

**Q：改寫會影響我的證據校驗嗎？**
不會。`guard_text` 只動文字，不動 artifact 裡的結構化欄位和證據編號。

**Q：為什麼不直接把 A5 的代碼合進來？**
兩邊資料模型不同（你是 job/artifact，A5 是 payload/evidence），合併會互相變形。
服務調服務是最輕的接法，也方便各自更新。

---

端點的完整格式（請求/回應欄位、錯錯誤碼）見 `a5-commander/docs/GUARD-API.md`。
