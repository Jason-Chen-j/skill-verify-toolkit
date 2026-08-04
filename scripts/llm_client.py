#!/usr/bin/env python3
"""OpenAI 相容伺服器的最小用戶端。純標準庫，免安裝套件。

設定來自環境變數，兩個都必填、沒有預設值——沒設就報錯，不替使用者猜。

- `LLM_API_BASE`，伺服器位址（含 `/v1`）。
- `LLM_MODEL`，模型名稱。

timeout 必須設。沒有 timeout 的批次呼叫卡住時不是「慢」，是無聲停住：
行程活著、log 零產出，從 ps 看不出來。
"""

import json
import os
import time
import urllib.error
import urllib.request

RETRY_STATUS = {429, 500, 502, 503, 504}


class LLMError(RuntimeError):
    """伺服器呼叫失敗。錯誤內文完整保留，不截斷。"""


def config():
    """讀環境變數，回傳 (base, model)。缺任一個直接報錯。"""
    base = os.environ.get("LLM_API_BASE", "").rstrip("/")
    model = os.environ.get("LLM_MODEL", "")
    if not base or not model:
        raise LLMError(
            "環境變數 LLM_API_BASE 與 LLM_MODEL 都必須設定，例如：\n"
            "  LLM_API_BASE=<伺服器位址>/v1 LLM_MODEL=<模型名> "
            "python3 scripts/run_cases.py <目錄>")
    return base, model


def chat(messages, *, model, response_format=None, tools=None,
         tool_choice=None, timeout=180.0, retries=2):
    """呼叫 /chat/completions，回傳解析後的完整回應 dict。

    暫時性失敗（連線錯誤、逾時、429、5xx）自動重試 retries 次。
    其他 HTTP 錯誤直接拋 LLMError，錯誤內文完整帶出。
    """
    base = os.environ.get("LLM_API_BASE", "").rstrip("/")
    if not base:
        raise LLMError("環境變數 LLM_API_BASE 未設定")
    body = {"model": model, "messages": messages}
    if response_format is not None:
        body["response_format"] = response_format
    if tools is not None:
        body["tools"] = tools
    if tool_choice is not None:
        body["tool_choice"] = tool_choice
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}

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
