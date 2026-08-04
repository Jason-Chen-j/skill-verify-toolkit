#!/usr/bin/env python3
"""第 2 關：實測。照 examples.json 逐案呼叫模型，寫出實測存證。

用法：
  LLM_MODEL=<模型> python3 scripts/run_cases.py <skill目錄或上層目錄...> \\
      [--out-dir 驗證報告] [--timeout 180] [--no-schema] [--skip-done]

行為：

- 有 `scripts/calc.py` 的 skill 走 tool-calling。模型呼叫 `run_calc`，
  本腳本實際執行 calc.py，並把每次呼叫記進存證的 `tool_calls`。
  「數值禁止心算」靠這個機制成立，判定器（J5）靠 tool_calls 驗數字有沒有被改掉。
- 存證寫 `<out-dir>/<skill目錄名>_驗證結果.json`，格式照《測試方法論.md》，不截斷。
- 預設以 response.json 做 json_schema strict 結構化輸出，與平台端一致。
  API 回 400 時多半是 response.json 不符 strict 要求（required 沒列全、
  缺 additionalProperties: false）——這是交付檔的缺陷訊號，記進 api_error 不靜默降級。
  伺服器不支援 json_schema 時加 `--no-schema`，schema 遵循改由第 3 關把關。
- 單一案例失敗記 `api_error` 後繼續，不中斷整批。
- 全程輸出同步寫 `<out-dir>/run_cases_全量.log`。
"""

import argparse
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import check_b_skill  # noqa: E402  複用 find_skill_dirs 與簡體字表
import llm_client  # noqa: E402

MAX_TOOL_ROUNDS = 4
CALC_TIMEOUT_SEC = 30

_LOG_PATH = None


def log(msg=""):
    print(msg, flush=True)
    if _LOG_PATH:
        with open(_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(msg + "\n")


# ── 提示與工具 ────────────────────────────────────────────────────────

def build_system_prompt(skill_md, response_schema, has_calc):
    parts = [skill_md, "\n\n---\n"]
    if has_calc:
        parts.append(
            "\n## 工具\n\n可用工具 `run_calc` 會執行本 skill 的 `scripts/calc.py`。"
            "**所有數字欄位一律填 `run_calc` 的回傳值，不得自行心算或改寫。**\n")
    parts.append(
        "\n## 輸出格式要求\n\n最後一則回覆必須是符合以下 JSON Schema 的純 JSON，"
        "不加任何解釋文字，不加 markdown 圍欄：\n\n```json\n"
        + json.dumps(response_schema, ensure_ascii=False, indent=2) + "\n```")
    return "".join(parts)


def tool_spec():
    return [{
        "type": "function",
        "function": {
            "name": "run_calc",
            "description": (
                "執行本 skill 的計算腳本 scripts/calc.py，回傳計算結果 JSON。"
                "所有數字欄位一律使用本工具的回傳值，不得自行心算。"),
            "parameters": {
                "type": "object",
                "properties": {
                    "payload": {
                        "type": "string",
                        "description": "要傳給腳本的 JSON 字串（腳本從 stdin 讀取）",
                    },
                },
                "required": ["payload"],
                "additionalProperties": False,
            },
        },
    }]


def run_calc(skill_dir, payload):
    """實際執行 calc.py。失敗要讓模型看得到錯誤，不可靜默回空。
    stderr 完整回傳不截斷——traceback 尾端才是根因。"""
    script = os.path.join(skill_dir, "scripts", "calc.py")
    try:
        proc = subprocess.run(
            [sys.executable, script],
            input=payload, capture_output=True, text=True,
            timeout=CALC_TIMEOUT_SEC)
    except subprocess.TimeoutExpired:
        return json.dumps({"error": f"腳本執行逾時（{CALC_TIMEOUT_SEC} 秒）"},
                          ensure_ascii=False)
    if proc.returncode != 0:
        return json.dumps(
            {"error": f"腳本 exit {proc.returncode}", "stderr": proc.stderr},
            ensure_ascii=False)
    return proc.stdout.strip() or json.dumps({"error": "腳本沒有輸出"},
                                             ensure_ascii=False)


def call_case(skill_dir, system_prompt, user_msg, response_schema,
              *, model, timeout, use_schema, has_calc):
    """跑完一組案例，回傳 (最終文字, tool_calls 紀錄, 實際模型名)。"""
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_msg},
    ]
    response_format = None
    if use_schema:
        response_format = {"type": "json_schema", "json_schema": {
            "name": "skill_output", "strict": True, "schema": response_schema}}
    trace = []
    reported_model = model

    rounds = MAX_TOOL_ROUNDS if has_calc else 1
    for _ in range(rounds):
        # 每一輪都帶 response_format：模型不呼叫工具的那一輪就是最終答案
        resp = llm_client.chat(
            messages, model=model, response_format=response_format,
            tools=tool_spec() if has_calc else None,
            tool_choice="auto" if has_calc else None, timeout=timeout)
        # model 欄位跟實際回應走，不寫死——寫死會在換模型時謊報
        reported_model = resp.get("model") or reported_model
        msg = llm_client.first_message(resp)
        tool_calls = msg.get("tool_calls") or []
        if not tool_calls:
            return (msg.get("content") or ""), trace, reported_model

        messages.append({
            "role": "assistant",
            "content": msg.get("content") or "",
            "tool_calls": [{
                "id": tc.get("id", ""), "type": "function",
                "function": {
                    "name": tc.get("function", {}).get("name", ""),
                    "arguments": tc.get("function", {}).get("arguments", "{}"),
                },
            } for tc in tool_calls],
        })
        for tc in tool_calls:
            try:
                payload = json.loads(
                    tc.get("function", {}).get("arguments", "{}")).get("payload", "{}")
            except (json.JSONDecodeError, AttributeError):
                payload = "{}"
            result = run_calc(skill_dir, payload)
            # 存證一律存完整內容。截斷的存證會讓下游檢查 parse 失敗後靜靜跳過
            trace.append({"payload": payload, "result": result})
            messages.append({"role": "tool", "tool_call_id": tc.get("id", ""),
                             "content": result})

    # 工具輪次用完仍未收斂，最後一輪不給工具、要求依 schema 產出
    messages.append({"role": "user", "content":
                     "請依前述工具回傳的數字，直接輸出最終 JSON，不要再呼叫工具。"})
    resp = llm_client.chat(messages, model=model,
                           response_format=response_format, timeout=timeout)
    reported_model = resp.get("model") or reported_model
    return (llm_client.first_message(resp).get("content") or ""), trace, reported_model


# ── 檢查 ──────────────────────────────────────────────────────────────

def try_parse_json(raw):
    raw = raw.strip()
    if raw.startswith("```"):
        lines = raw.split("\n")
        end = len(lines)
        for i in range(1, len(lines)):
            if lines[i].strip() == "```":
                end = i
                break
        raw = "\n".join(lines[1:end])
    try:
        return json.loads(raw), True
    except json.JSONDecodeError:
        return None, False


def simplified_hits(text):
    return sum(1 for ch in text
               if ch in check_b_skill.SIMPLIFIED_CHARS
               and ch not in check_b_skill.SIMPLIFIED_SAFE)


def schema_check(parsed, response_schema):
    target = parsed[0] if isinstance(parsed, list) and parsed else parsed
    if not isinstance(target, dict):
        return "", False
    required = response_schema.get("required", [])
    missing = [k for k in required if k not in target]
    text = f"必填 {len(required) - len(missing)}/{len(required)}"
    if missing:
        text += f"，缺: {missing}"
    return text, not missing


# ── 主流程 ────────────────────────────────────────────────────────────

def verify_skill(skill_dir, out_dir, *, model, timeout, use_schema):
    name = os.path.basename(skill_dir.rstrip("/"))
    required_files = ("SKILL.md", os.path.join("schemas", "response.json"),
                      "examples.json")
    missing = [f for f in required_files
               if not os.path.isfile(os.path.join(skill_dir, f))]
    if missing:
        # 半成品目錄要點名跳過，不能 traceback 中斷整批
        log(f"跳過（缺 {', '.join(missing)}）：{name}")
        return None

    with open(os.path.join(skill_dir, "SKILL.md"), encoding="utf-8") as f:
        skill_md = f.read()
    with open(os.path.join(skill_dir, "schemas", "response.json"),
              encoding="utf-8") as f:
        response_schema = json.load(f)
    with open(os.path.join(skill_dir, "examples.json"), encoding="utf-8") as f:
        examples = json.load(f)
    has_calc = os.path.isfile(os.path.join(skill_dir, "scripts", "calc.py"))

    system_prompt = build_system_prompt(skill_md, response_schema, has_calc)
    mode = "工具" if has_calc else "純生成"
    # 每支帶時間戳：卡住時光看 log 就知道停多久
    log(f"\n{'=' * 60}")
    log(f"實測（{mode}）：{name}  [{time.strftime('%H:%M:%S')}]")
    log(f"{'=' * 60}")

    results = []
    for i, ex in enumerate(examples, 1):
        uq = ex.get("user_query", "")
        inp = ex.get("input", {})
        user_msg = uq
        if inp:
            user_msg += "\n\n輸入資料：\n" + json.dumps(
                inp, ensure_ascii=False, indent=2)
        log(f"  [{i}/{len(examples)}] {uq[:40]}  [{time.strftime('%H:%M:%S')}]")
        start = time.time()
        try:
            raw, trace, reported = call_case(
                skill_dir, system_prompt, user_msg, response_schema,
                model=model, timeout=timeout, use_schema=use_schema,
                has_calc=has_calc)
            api_error = None
        except llm_client.LLMError as exc:
            raw, trace, reported = "", [], model
            api_error = str(exc)
            log(f"    ❌ API 失敗：{api_error}")
        elapsed = round(time.time() - start, 1)

        parsed, parse_ok = try_parse_json(raw)
        check_text, _ = schema_check(parsed, response_schema) if parse_ok else ("", False)
        simp = simplified_hits(raw)
        entry = {
            "test": f"測試{i}_{uq[:30]}",
            "model": reported,
            "input": inp,
            "user_query": uq,
            "raw_response": raw,
            "parse_ok": parse_ok,
            "schema_check": check_text,
            "checks": {"simplified_hits": simp},
            "tool_calls": trace,
            "elapsed_sec": elapsed,
        }
        if api_error:
            entry["api_error"] = api_error
        results.append(entry)
        if not api_error:
            log(f"    JSON={parse_ok}｜工具呼叫 {len(trace)} 次｜"
                f"簡體={simp}｜{check_text}｜{elapsed}s")

    out_path = os.path.join(out_dir, f"{name}_驗證結果.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    log(f"  → 存證：{out_path}")
    return results


def main():
    global _LOG_PATH
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="+", help="skill 目錄或上層目錄")
    ap.add_argument("--out-dir", default="驗證報告")
    ap.add_argument("--timeout", type=float, default=180.0,
                    help="單次 API 呼叫逾時秒數（預設 180）")
    ap.add_argument("--no-schema", action="store_true",
                    help="不送 json_schema 結構化輸出（伺服器不支援時用）")
    ap.add_argument("--skip-done", action="store_true",
                    help="已有存證檔的 skill 跳過（續跑用）")
    args = ap.parse_args()

    try:
        _, model = llm_client.config()
    except llm_client.LLMError as exc:
        print(exc, file=sys.stderr)
        sys.exit(1)
    os.makedirs(args.out_dir, exist_ok=True)
    _LOG_PATH = os.path.join(args.out_dir, "run_cases_全量.log")

    skill_dirs = check_b_skill.find_skill_dirs(args.paths)
    if not skill_dirs:
        log("找不到任何 skill 目錄（判定依據：目錄含 tool.yaml）")
        sys.exit(1)

    log(f"實測開始：{len(skill_dirs)} 支｜模型 {model}｜"
        f"schema {'關閉' if args.no_schema else '啟用'}｜{time.strftime('%F %T')}")
    summary = []
    for skill_dir in skill_dirs:
        name = os.path.basename(skill_dir.rstrip("/"))
        out_path = os.path.join(args.out_dir, f"{name}_驗證結果.json")
        if args.skip_done and os.path.isfile(out_path):
            log(f"沿用既有存證：{name}")
            continue
        results = verify_skill(skill_dir, args.out_dir, model=model,
                               timeout=args.timeout,
                               use_schema=not args.no_schema)
        if results is not None:
            summary.append((name, results))

    log(f"\n{'=' * 60}\n總計\n{'=' * 60}")
    error_rows = []
    for name, results in summary:
        ok = sum(1 for r in results if r["parse_ok"])
        used = sum(1 for r in results if r["tool_calls"])
        log(f"  {name[:40]:<40} JSON OK {ok}/{len(results)}｜有呼叫腳本 {used}/{len(results)}")
        error_rows += [(name, r["test"], r["api_error"])
                       for r in results if r.get("api_error")]
    if error_rows:
        log(f"\nAPI 失敗 {len(error_rows)} 筆（多半是 response.json 不符 strict 要求，"
            "屬交付檔缺陷訊號）：")
        for name, test, err in error_rows:
            log(f"  ❌ {name} / {test}\n     {err}")
    log(f"\n下一步：用另一個模型跑 scripts/judge_llm.py 產出品質判定檔，"
        f"再跑 run_verify.py --mode judge。")


if __name__ == "__main__":
    main()
