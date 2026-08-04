#!/usr/bin/env python3
"""第 3 關：品質判定。呼叫模型當閱卷者，逐案評分，寫出 `<skill>_品質判定.json`。

用法：
  LLM_MODEL=<判定模型> python3 scripts/judge_llm.py <skill目錄或上層目錄...> \\
      [--out-dir 驗證報告] [--timeout 300] [--no-schema] [--skip-done]

分工原則：**分數的算術不交給模型。** 模型只給五維度分數與問題清單；
`total_score`、`average_score`（取 2 位小數）、`verdict` 由本腳本依 judge.py
的同一套規則計算。模型加總錯誤、平均取錯位數這一類缺陷從機制上排除。

判定模型應與實測存證裡的模型不同（換 `LLM_MODEL`）。相同時印警告——
自己改自己的考卷，判定獨立性不足。

評分規則即 `judge.py` 的 RUBRIC 段，本腳本直接引用，不另抄一份。
全程輸出同步寫 `<out-dir>/judge_llm_全量.log`。
"""

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import check_b_skill  # noqa: E402  複用 find_skill_dirs
import judge  # noqa: E402  RUBRIC、五維度、四級判定規則的唯一出處
import llm_client  # noqa: E402

EVIDENCE_SUFFIXES = ("_驗證結果_工具版.json", "_驗證結果.json")

_LOG_PATH = None


def log(msg=""):
    print(msg, flush=True)
    if _LOG_PATH:
        with open(_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(msg + "\n")


# ── 判定請求 ──────────────────────────────────────────────────────────

def judgment_schema(case_count):
    dims = list(judge.QUALITY_DIMENSIONS)
    return {
        "type": "object",
        "properties": {
            "cases": {
                "type": "array",
                "minItems": case_count,
                "maxItems": case_count,
                "items": {
                    "type": "object",
                    "properties": {
                        "case": {"type": "integer"},
                        "scores": {
                            "type": "object",
                            "properties": {d: {"type": "number"} for d in dims},
                            "required": dims,
                            "additionalProperties": False,
                        },
                        "critical_issues": {"type": "array",
                                            "items": {"type": "string"}},
                        "tolerable_issues": {"type": "array",
                                             "items": {"type": "string"}},
                        "findings": {"type": "array",
                                     "items": {"type": "string"}},
                    },
                    "required": ["case", "scores", "critical_issues",
                                 "tolerable_issues", "findings"],
                    "additionalProperties": False,
                },
            },
            "summary": {"type": "string"},
        },
        "required": ["cases", "summary"],
        "additionalProperties": False,
    }


def build_messages(skill_md, cases):
    system = judge.RUBRIC + (
        "\n\n## 本次作業方式\n\n"
        "你只回一份 JSON，結構是 {\"cases\": [...], \"summary\": \"...\"}。\n"
        "每案給 `case`（案例編號）、`scores`（五維度各 0–20）、`critical_issues`、"
        "`tolerable_issues`、`findings`（都是文字陣列，findings 要含可核對的依據，"
        "例如數字對照或條文出處）。\n"
        "不要計算 total_score、average_score、verdict——由程式計算。\n"
        "只根據可核對的資料判定，不確定的不寫。")
    blocks = ["# 受測 skill 的 SKILL.md\n", skill_md, "\n\n# 實測存證（逐案）\n"]
    for i, case in enumerate(cases, 1):
        blocks.append(f"\n## 案例 {i}\n")
        blocks.append("user_query：" + str(case.get("user_query", "")) + "\n")
        blocks.append("input：\n```json\n" + json.dumps(
            case.get("input", {}), ensure_ascii=False, indent=2) + "\n```\n")
        tool_calls = case.get("tool_calls") or []
        if tool_calls:
            blocks.append("tool_calls（腳本實際輸入與輸出）：\n```json\n" + json.dumps(
                tool_calls, ensure_ascii=False, indent=2) + "\n```\n")
        blocks.append("raw_response：\n```\n"
                      + str(case.get("raw_response", "")) + "\n```\n")
    return [{"role": "system", "content": system},
            {"role": "user", "content": "".join(blocks)}]


def validate_judgment(data, case_count):
    """驗模型回覆的結構。回傳錯誤訊息清單，空清單＝合格。"""
    errors = []
    if not isinstance(data, dict):
        return ["回覆不是 JSON 物件"]
    cases = data.get("cases")
    if not isinstance(cases, list) or len(cases) != case_count:
        return [f"cases 必須是 {case_count} 筆的陣列，"
                f"實際 {len(cases) if isinstance(cases, list) else '非陣列'}"]
    dims = set(judge.QUALITY_DIMENSIONS)
    for i, case in enumerate(cases, 1):
        if not isinstance(case, dict):
            errors.append(f"案例{i} 不是物件")
            continue
        scores = case.get("scores")
        if not isinstance(scores, dict) or set(scores) != dims:
            errors.append(f"案例{i} scores 必須恰好包含五個固定維度")
            continue
        for key, value in scores.items():
            if not isinstance(value, (int, float)) or not 0 <= value <= 20:
                errors.append(f"案例{i}「{key}」必須是 0 到 20 的數字")
        for field in ("critical_issues", "tolerable_issues", "findings"):
            value = case.get(field)
            if not isinstance(value, list) or not all(
                    isinstance(item, str) for item in value):
                errors.append(f"案例{i} {field} 必須是文字陣列")
    if not isinstance(data.get("summary"), str):
        errors.append("summary 必須是文字")
    return errors


def assemble(skill_name, judge_model, raw_cases, summary):
    """由模型給的維度分數計算 total/average/verdict，組出判定檔內容。"""
    cases = []
    totals = []
    all_critical = []
    for i, case in enumerate(raw_cases, 1):
        scores = case["scores"]
        total = round(sum(float(scores[d]) for d in judge.QUALITY_DIMENSIONS), 2)
        totals.append(total)
        all_critical += case["critical_issues"]
        cases.append({
            "case": i,
            "scores": scores,
            "total_score": total,
            "critical_issues": case["critical_issues"],
            "tolerable_issues": case["tolerable_issues"],
            "findings": case["findings"],
        })
    average = round(sum(totals) / len(totals), 2)
    return {
        "version": 2,
        "skill": skill_name,
        "judge_model": judge_model,
        "criteria": list(judge.QUALITY_DIMENSIONS),
        "cases": cases,
        "average_score": average,
        "critical_issues": [],
        "tolerable_issues": [],
        "verdict": judge.quality_outcome(average, all_critical),
        "summary": summary,
    }


# ── 主流程 ────────────────────────────────────────────────────────────

def load_evidence(skill_name, out_dir):
    """讀存證。工具版在前、文字版在後，與 judge.py 的讀取順序一致。"""
    cases = []
    models = set()
    for suffix in EVIDENCE_SUFFIXES:
        path = os.path.join(out_dir, skill_name + suffix)
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        for case in data if isinstance(data, list) else []:
            cases.append(case)
            if case.get("model"):
                models.add(case["model"])
    return cases, models


def judge_one(skill_dir, out_dir, *, model, timeout, use_schema):
    name = os.path.basename(skill_dir.rstrip("/"))
    cases, evidence_models = load_evidence(name, out_dir)
    if not cases:
        log(f"跳過（{out_dir} 沒有 {name} 的存證檔，先跑 run_cases.py）：{name}")
        return None
    if model in evidence_models:
        log(f"⚠️  {name}：判定模型與存證模型相同（{model}），判定獨立性不足。"
            "建議換 LLM_MODEL 重跑。")

    with open(os.path.join(skill_dir, "SKILL.md"), encoding="utf-8") as f:
        skill_md = f.read()

    log(f"判定：{name}（{len(cases)} 案）  [{time.strftime('%H:%M:%S')}]")
    messages = build_messages(skill_md, cases)
    response_format = None
    if use_schema:
        response_format = {"type": "json_schema", "json_schema": {
            "name": "quality_judgment", "strict": True,
            "schema": judgment_schema(len(cases))}}

    judge_model = model
    data = None
    # 結構不合格時帶著錯誤訊息重問一次；再不合格就回報失敗，不硬修
    for attempt in range(2):
        resp = llm_client.chat(messages, model=model,
                               response_format=response_format, timeout=timeout)
        judge_model = resp.get("model") or judge_model
        raw = llm_client.first_message(resp).get("content") or ""
        try:
            data = json.loads(raw)
            errors = validate_judgment(data, len(cases))
        except json.JSONDecodeError as exc:
            errors = [f"回覆不是合法 JSON：{exc}"]
        if not errors:
            break
        data = None
        if attempt == 0:
            log(f"    回覆結構不合格，重問一次：{'；'.join(errors)}")
            messages.append({"role": "assistant", "content": raw})
            messages.append({"role": "user", "content":
                             "上一份回覆結構不合格：" + "；".join(errors)
                             + "。請重出完整 JSON。"})
        else:
            log(f"    ❌ 重問後仍不合格：{'；'.join(errors)}")
    if data is None:
        return None

    judgment = assemble(name, judge_model, data["cases"], data["summary"])
    out_path = os.path.join(out_dir, f"{name}_品質判定.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(judgment, f, ensure_ascii=False, indent=2)
    log(f"    {judgment['verdict']}｜平均 {judgment['average_score']}｜→ {out_path}")
    return judgment


def main():
    global _LOG_PATH
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="+", help="skill 目錄或上層目錄")
    ap.add_argument("--out-dir", default="驗證報告",
                    help="存證所在目錄，判定檔也寫在這裡（預設 驗證報告）")
    ap.add_argument("--timeout", type=float, default=300.0,
                    help="單次 API 呼叫逾時秒數（預設 300，判定要讀長存證）")
    ap.add_argument("--no-schema", action="store_true",
                    help="不送 json_schema 結構化輸出（伺服器不支援時用）")
    ap.add_argument("--skip-done", action="store_true",
                    help="已有品質判定檔的 skill 跳過（續跑用）")
    args = ap.parse_args()

    try:
        _, model = llm_client.config()
    except llm_client.LLMError as exc:
        print(exc, file=sys.stderr)
        sys.exit(1)
    os.makedirs(args.out_dir, exist_ok=True)
    _LOG_PATH = os.path.join(args.out_dir, "judge_llm_全量.log")

    skill_dirs = check_b_skill.find_skill_dirs(args.paths)
    if not skill_dirs:
        log("找不到任何 skill 目錄（判定依據：目錄含 tool.yaml）")
        sys.exit(1)

    log(f"品質判定開始：{len(skill_dirs)} 支｜判定模型 {model}｜{time.strftime('%F %T')}")
    done = failed = 0
    for skill_dir in skill_dirs:
        name = os.path.basename(skill_dir.rstrip("/"))
        out_path = os.path.join(args.out_dir, f"{name}_品質判定.json")
        if args.skip_done and os.path.isfile(out_path):
            log(f"沿用既有判定：{name}")
            continue
        try:
            result = judge_one(skill_dir, args.out_dir, model=model,
                               timeout=args.timeout,
                               use_schema=not args.no_schema)
        except llm_client.LLMError as exc:
            # 單一 skill 失敗不得中斷整批
            log(f"    ❌ API 失敗：{exc}")
            result = None
        if result is None:
            failed += 1
        else:
            done += 1

    log(f"\n完成 {done} 支、失敗或缺存證 {failed} 支。")
    log("下一步：python3 run_verify.py <目錄> --mode judge --out-dir "
        f"{args.out_dir}，產出《驗證總覽.md》與《驗證明細.md》。")


if __name__ == "__main__":
    main()
