#!/usr/bin/env python3
"""Skill 驗證總入口 — 一包帶走。

三關：
  1. 機測 scripts/check_b_skill.py（零 LLM、零額度，任何有 python3 的電腦都能跑）
  2. 模型自測：由執行本工具包的模型照包內「測試方法論.md」執行——
     自己逐支逐案測，或開 agent 測。存證與品質判定檔寫進 <out-dir>
  3. 判定 scripts/judge.py：吃第 2 關的存證與品質判定檔，判它過不過。

第 3 關是有存證才跑得動的。第一次跑會在第 2 關停下來等存證，
存證放回報告目錄之後再跑一次，第 3 關才有東西可判。

用法：
  python3 run_verify.py <skill目錄或上層目錄...> [--out-dir 報告目錄]
  python3 run_verify.py <skill目錄或上層目錄...> --out-dir 驗證報告 --new-run
  python3 run_verify.py <目錄> --mode mech            只跑機測
  python3 run_verify.py <目錄> --mode judge           只跑判定（存證已就位時用）
  python3 run_verify.py <目錄> --skip-done            已有存證的支數不列入方法論

主要報告固定為兩份：驗證總覽.md 與 驗證明細.md。
受測的 skill 目錄不寫入任何檔案。
全程輸出同步寫入 <out-dir>/run_verify_全量.log（不截斷）。

--new-run 會建立新的報告目錄。同名目錄已存在時，依序使用
「_重複驗證_01」、「_重複驗證_02」等名稱；實際使用的目錄會顯示在執行結果第一行。
"""

import argparse
import json
import os
import shutil
import subprocess
import sys

PKG = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(PKG, "scripts")
sys.path.insert(0, SCRIPTS)

import check_b_skill  # noqa: E402  複用 find_skill_dirs

_LOG_PATH = None


def log(msg=""):
    print(msg, flush=True)
    if _LOG_PATH:
        with open(_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(msg + "\n")


# ── 機測 ──────────────────────────────────────────────────────────

def run_mech(targets, out_dir):
    log("=" * 60)
    log("第一關：機測（check_b_skill.py）")
    log("=" * 60)
    proc = subprocess.run(
        [sys.executable, os.path.join(SCRIPTS, "check_b_skill.py"),
         *targets, "--out-dir", out_dir],
        capture_output=True, text=True, check=False)
    log(proc.stdout.rstrip())
    if proc.stderr.strip():
        log("[stderr] " + proc.stderr.strip())
    return proc.returncode


def run_judge(targets, out_dir):
    """第三關：吃第二關的存證，判它過不過。沒有存證就只會印一排「待補驗」。"""
    formatter = subprocess.run(
        [sys.executable, os.path.join(SCRIPTS, "format_evidence.py"), out_dir],
        capture_output=True, text=True, check=False)
    if formatter.stdout.strip():
        log(formatter.stdout.rstrip())
    if formatter.stderr.strip():
        log("[格式化存證 stderr] " + formatter.stderr.strip())
    if formatter.returncode:
        log("[格式化存證] 有檔案無法格式化；仍保留原檔並繼續判定。")
    log("")
    log("=" * 60)
    log("第三關：判定（judge.py）")
    log("=" * 60)
    proc = subprocess.run(
        [sys.executable, os.path.join(SCRIPTS, "judge.py"),
         *targets, "--out-dir", out_dir, "--evidence-dir", out_dir],
        capture_output=True, text=True, check=False)
    log(proc.stdout.rstrip())
    if proc.stderr.strip():
        log("[stderr] " + proc.stderr.strip())
    return proc.returncode


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def new_run_dir(out_dir):
    """回傳尚未使用的報告目錄，保留每一輪存證與判定資料。"""
    base = out_dir.rstrip(os.sep)
    if not os.path.exists(base):
        return base
    index = 1
    while True:
        candidate = f"{base}_重複驗證_{index:02d}"
        if not os.path.exists(candidate):
            return candidate
        index += 1


def write_consolidated_reports(out_dir):
    """保留兩份人讀報告；機械與判定的中間檔移到歷史資料夾。"""
    judge_path = os.path.join(out_dir, "判定報告.json")
    detail_path = os.path.join(out_dir, "判定報告.md")
    data_path = os.path.join(out_dir, "驗證資料.json")
    mech_path = os.path.join(out_dir, "機械檢查_報告.json")
    if not (os.path.isfile(judge_path) and os.path.isfile(detail_path)):
        return

    try:
        judgments = read_json(judge_path)
        if os.path.isfile(mech_path):
            mechanical = read_json(mech_path)
        elif os.path.isfile(data_path):
            mechanical = read_json(data_path).get("機械檢查", {})
        else:
            mechanical = {}
    except (OSError, json.JSONDecodeError) as exc:
        log(f"無法整合報告：{exc}")
        return

    results = mechanical.get("results", []) if isinstance(mechanical, dict) else []
    errors = sum(r.get("errors", 0) for r in results if isinstance(r, dict))
    warnings = sum(r.get("warns", 0) for r in results if isinstance(r, dict))
    states = ("可直接交付", "通過，有可忍受問題", "需修正", "品質未判定", "待補驗", "不通過")
    counts = {state: sum(1 for r in judgments if r.get("verdict") == state)
              for state in states}
    scores = [r.get("quality_score") for r in judgments
              if isinstance(r.get("quality_score"), (int, float))]
    average = sum(scores) / len(scores) if scores else None

    overview = [
        "# Skill 驗證總覽", "", "## 結論",
        f"- 受測 Skill：{len(judgments)} 支",
        f"- 可直接交付：{counts['可直接交付']} 支",
        f"- 通過，有可忍受問題：{counts['通過，有可忍受問題']} 支",
        f"- 需修正：{counts['需修正']} 支",
        f"- 品質未判定：{counts['品質未判定']} 支",
        f"- 待補驗：{counts['待補驗']} 支",
        f"- 不通過：{counts['不通過']} 支",
        f"- 機械檢查錯誤：{errors}",
        f"- 機械檢查警告：{warnings}",
    ]
    if average is not None:
        overview.append(f"- 內容品質平均分數：{average:.1f}/100")
    overview += [
        "", "## 驗證範圍",
        "- 機械檢查：檔案、設定、Schema、語言與去識別化。",
        "- 實測：每支 Skill 依 examples.json 的案例產生輸出並保存。",
        "- 內容品質：模型依 Skill 規格、輸入資料與實際輸出，以五個 20 分維度逐案評分。",
        "- 嚴重問題：金額、法規、期限、資格或當事人資料錯誤時，判定為不通過。",
        "- 分數判定：90 分以上可直接交付；80 至 89 分通過且列出可忍受問題；60 至 79 分需修正。",
        "", "## 詳細結果",
        "請閱讀 `驗證明細.md`。逐案輸出與判定資料在 `驗證資料.json`。",
    ]
    with open(os.path.join(out_dir, "驗證總覽.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(overview) + "\n")

    with open(detail_path, encoding="utf-8") as f:
        detail = f.read().replace("# 判定報告", "# Skill 驗證明細", 1)
    if results:
        mechanical_lines = ["", "## 機械檢查明細"]
        for result in results:
            if not isinstance(result, dict):
                continue
            name = result.get("name", "未命名 Skill")
            errors_for_skill = result.get("errors", 0)
            warnings_for_skill = result.get("warns", 0)
            mechanical_lines.append(
                f"### {name}：錯誤 {errors_for_skill}，警告 {warnings_for_skill}")
            for finding in result.get("findings", []):
                if isinstance(finding, list) and len(finding) == 3:
                    level, code, message = finding
                    mechanical_lines.append(f"- {level} [{code}]：{message}")
        detail += "\n".join(mechanical_lines) + "\n"
    with open(os.path.join(out_dir, "驗證明細.md"), "w", encoding="utf-8") as f:
        f.write(detail)
    with open(data_path, "w", encoding="utf-8") as f:
        json.dump({"機械檢查": mechanical, "判定": judgments}, f,
                  ensure_ascii=False, indent=2)

    history = os.path.join(out_dir, "歷史報告")
    os.makedirs(history, exist_ok=True)
    for name in ("機械檢查_報告.md", "機械檢查_報告.json", "判定報告.md",
                 "判定報告.json"):
        source = os.path.join(out_dir, name)
        if os.path.isfile(source):
            shutil.move(source, os.path.join(history, name))
    log("已產出：驗證總覽.md、驗證明細.md、驗證資料.json")


# ── 主流程 ────────────────────────────────────────────────────────

def main():
    global _LOG_PATH
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("paths", nargs="*")
    ap.add_argument("--out-dir", default="驗證報告")
    ap.add_argument("--new-run", action="store_true")
    ap.add_argument("--mode", choices=["auto", "mech", "judge"], default="auto")
    ap.add_argument("--skip-done", action="store_true")
    ap.add_argument("-h", "--help", action="store_true")
    args = ap.parse_args()

    if args.help or not args.paths:
        print(__doc__)
        return 0

    if args.new_run:
        args.out_dir = new_run_dir(args.out_dir)
    os.makedirs(args.out_dir, exist_ok=True)
    _LOG_PATH = os.path.join(args.out_dir, "run_verify_全量.log")
    open(_LOG_PATH, "w", encoding="utf-8").close()

    skill_dirs = check_b_skill.find_skill_dirs(args.paths)
    if not skill_dirs:
        log("找不到任何含 tool.yaml 的目錄")
        return 2
    log(f"目標 {len(skill_dirs)} 支 skill｜報告目錄：{args.out_dir}｜log：{_LOG_PATH}\n")

    if args.mode == "judge":
        rc_judge = run_judge(args.paths, args.out_dir)
        write_consolidated_reports(args.out_dir)
        return rc_judge

    rc_mech = run_mech(args.paths, args.out_dir)
    if args.mode == "mech":
        return rc_mech

    log("")
    log("=" * 60)
    log("第二關：實測（模型自測）")
    log("=" * 60)
    todo = list(skill_dirs)
    if args.skip_done:
        todo = []
        for d in skill_dirs:
            n = os.path.basename(d.rstrip("/"))
            if os.path.isfile(os.path.join(args.out_dir, f"{n}_驗證結果.json")):
                log(f"  已有存證，跳過：{n}")
            else:
                todo.append(d)
    if todo:
        log(f"有 {len(todo)} 支 Skill 尚未建立實測存證：")
        for d in todo:
            log(f"  - {os.path.abspath(d)}")
        log(f"測試模型照 {os.path.join(PKG, '測試方法論.md')} 執行，"
            "存證與品質判定檔寫入報告目錄後再重跑本指令。")
    else:
        log("全部支數都有存證，不需要再測。")

    rc_judge = run_judge(args.paths, args.out_dir)
    write_consolidated_reports(args.out_dir)

    log(f"\n完成。機測 exit={rc_mech}｜判定 exit={rc_judge}｜全量 log：{_LOG_PATH}")
    if todo:
        log("※ 還有支數沒有存證，判定結果會是「待補驗」；補完存證要重跑。")
    return rc_mech or rc_judge


if __name__ == "__main__":
    sys.exit(main())
