# PrismQuant A5 指揮官智能體（Commander Agent）

唯一的用戶入口：呼叫 A1 新聞與 A2 政要監控，把兩邊結論**匯總、調解分歧**，
產出一份 10 分鐘讀得完的**晨報＋證據鏈**。A3 執行、A4 風控接入後，再擴充它的匯總範圍。

## 技術棧

- FastAPI（無狀態，不存資料庫）
- 用 `httpx` 呼叫上游 A1 / A2 的 HTTP 接口
- 大模型：OpenRouter（OpenAI 相容介面）

## 快速開始

```bash
# 1. 建虛擬環境並安裝
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt

# 2. 設定
copy .env.example .env
# 編輯 .env：填入 PQ_LLM_API_KEY，並確認 PQ_A1_API_URL / PQ_A2_API_URL 指向正確地址

# 3. 啟動（請先啟動 A1 與 A2）
.venv\Scripts\python -m uvicorn app.main:app --host 127.0.0.1 --port 8130
```

## 接口

| 方法 | 路徑 | 說明 |
|---|---|---|
| GET | `/health` | 服務是否活著 |
| POST | `/api/v1/commander/run` | 傳 `{"question":"今天該留意什麼風險？"}`，回晨報＋證據鏈 |

互動文件：`http://127.0.0.1:8130/docs`。

## 幾點說明

1. **A5 依賴 A1、A2**：要先把它們開起來；若 A1/A2 沒開，`upstream_ok` 會標 `false`，晨報仍會產出並註明缺哪些。
2. **密鑰安全**：`.env` 已被 `.gitignore` 擋住。
