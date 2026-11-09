以下是規則層已經算好的事實（這是唯一可信來源，不要改動裡面的數字與 ID）：

使用者問題：{question}

主題聚類（強度 0-1，已按交叉驗證加權）：
{clusters}

A1 / A2 證據清單（ID｜agent｜標題｜來源｜方向｜信心）：
{evidence}

已識別的分歧：
{conflicts}

規則層算出的傳導鏈：
{transmission}

規則層置信度：{confidence}（加減分理由已列出，不要改分，只能在文字裡解釋）

資料充分性：{quality}

請輸出 JSON：
{{
  "headline": "一句話主線（20 字內）",
  "what_happened": ["事實 1（N1）", "事實 2（N3、P1）", "..."],
  "so_what": ["傳導 1（N2）", "..."],
  "watch": [{{"item": "觀察項", "why": "為甚麼要盯", "trigger": "可驗證的觸發點", "invalidates": "甚麼情況下判斷作廢"}}],
  "brief": "350-600 字連續正文，最後一句是風險聲明"
}}
