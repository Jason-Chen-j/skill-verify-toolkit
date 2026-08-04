#!/usr/bin/env python3
"""OpenAI 相容 API 的最小用戶端。純標準庫，免安裝套件。

設定來自環境變數。

- `LLM_API_BASE`，API 位址，預設 `https://api.openai.com/v1`。自架或相容伺服器換成自己的位址。
- `LLM_API_KEY`，金鑰。伺服器不驗金鑰時可不設。
- `LLM_MODEL`，模型名稱。必填，沒有預設值——沒設就報錯，不替使用者猜模型。

timeout 必須設。沒有 timeout 的批次呼叫卡住時不是「慢」，是無聲停住：
行程活著、log 零產出，從 ps 看不出來。
"""

import json
import os
import time
import urllib.error
import urllib.request

DEFAULT_BASE = "https://api.openai.com/v1"
RETRY_STATUS = {429, 500, 502, 503, 504}


class LLMError(RuntimeError):
    """API 呼叫失敗。錯誤內文完整保留，不截斷。"""


def config():
    """讀環境變數，回傳 (base, key, model)。model 沒設直接報錯。"""
    base = os.environ.get("LLM_API_BASE", DEFAULT_BASE).rstrip("/")
    key = os.environ.get("LLM_API_KEY", "")
    model = os.environ.get("LLM_MODEL", "")
    if not model:
        raise LLMError(
            "環境變數 LLM_MODEL 未設定。請指定模型名稱，例如：\n"
            "  LLM_MODEL=<模型名> LLM_API_KEY=<金鑰> python3 scripts/run_cases.py <目錄>")
    return base, key, model


def chat(messages, *, model, response_format=None, tools=None,
         tool_choice=None, timeout=180.0, retries=2):
    """呼叫 /chat/completions，回傳解析後的完整回應 dict。

    暫時性失敗（連線錯誤、逾時、429、5xx）自動重試 retries 次。
    其他 HTTP 錯誤直接拋 LLMError，錯誤內文完整帶出。
    """
    base, key, _ = os.environ.get("LLM_API_BASE", DEFAULT_BASE).rstrip("/"), \
        os.environ.get("LLM_API_KEY", ""), None
    body = {"model": model, "messages": messages}
    if response_format is not None:
        body["response_format"] = response_format
    if tools is not None:
        body["tools"] = tools
    if tool_choice is not None:
        body["tool_choice"] = tool_choice
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"

    last_error = "未知錯誤"
    for attempt in range(retries + 1):
        request = urllib.request.Request(
            f"{base}/chat/completions", data=payload, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            last_error = f"HTTP {exc.code}：{detail}"
            if exc.code not in RETRY_STATUS:
                raise LLMError(last_error) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        if attempt < retries:
            time.sleep(5 * (attempt + 1))
    raise LLMError(f"重試 {retries} 次後仍失敗：{last_error}")


def first_message(resp):
    """取回應的第一則 message dict。結構不對時拋錯，不靜默回空。"""
    try:
        return resp["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMError(
            "回應結構不含 choices[0].message，完整回應：\n"
            + json.dumps(resp, ensure_ascii=False, indent=2)) from exc
