#!/usr/bin/env python3
"""calc.py 回歸測試：驗證型別標註不影響 calc.py 行為。

基準來自 `驗證結果_工具版.json` 裡的 `tool_calls`，包含模型實際送進 calc.py 的
參數（payload）與 calc.py 輸出（result）。
把同樣的 payload 再餵一次，輸出必須逐鍵相同。

用法：
    python3 scripts/regress_calc.py <skill目錄路徑>
    python3 scripts/regress_calc.py --all

離開碼：0 = 全部一致；1 = 有差異或執行失敗。差異會逐鍵印出來，不只印「不一致」。
"""
import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import cast

_json_loads: Callable[[str], object] = json.loads

BASE = Path(__file__).resolve().parent.parent
EVIDENCE = "驗證結果_工具版.json"

# 存證檔集中放在一個目錄，skill 目錄只留交付檔。
# 用環境變數 SKILL_EVIDENCE_DIR 指定該目錄（例如驗證報告目錄）。
EVIDENCE_DIR = os.environ.get("SKILL_EVIDENCE_DIR", "")


def evidence_path(skill_dir: Path, name: str) -> Path:
    """存證檔的位置：`<存證目錄>/<skill目錄名>_<檔名>`。

    找不到就退回 skill 目錄底下。兩種擺法都支援，
    換位置不會讓整批變成「查無存證」——那種假結果比報錯還糟。
    """
    if EVIDENCE_DIR:
        p = Path(EVIDENCE_DIR) / f"{skill_dir.name}_{name}"
        if p.is_file():
            return p
    return skill_dir / name


def as_obj(text: str) -> object:
    return _json_loads(text)


def diff_keys(want: object, got: object, path: str = "") -> list[str]:
    """逐鍵比對兩個 JSON 值，回傳所有不一致的路徑與新舊值。"""
    if isinstance(want, dict) and isinstance(got, dict):
        w = cast("dict[str, object]", want)
        g = cast("dict[str, object]", got)
        out: list[str] = []
        for k in sorted(set(w) | set(g)):
            if k not in w:
                out.append(f"{path}.{k}：改前沒有，改後多出 {g[k]!r}")
            elif k not in g:
                out.append(f"{path}.{k}：改前有 {w[k]!r}，改後不見了")
            else:
                out.extend(diff_keys(w[k], g[k], f"{path}.{k}"))
        return out
    if isinstance(want, list) and isinstance(got, list):
        wl = cast("list[object]", want)
        gl = cast("list[object]", got)
        if len(wl) != len(gl):
            return [f"{path}：陣列長度 改前 {len(wl)} → 改後 {len(gl)}"]
        out = []
        for i, (a, b) in enumerate(zip(wl, gl)):
            out.extend(diff_keys(a, b, f"{path}[{i}]"))
        return out
    if want != got:
        return [f"{path}：改前 {want!r} → 改後 {got!r}"]
    return []


def run_one(skill_dir: Path) -> tuple[int, int, list[str]]:
    """跑一支 skill 的全部案例，回傳（比對數, 一致數, 問題清單）。"""
    calc = skill_dir / "scripts" / "calc.py"
    ev = evidence_path(skill_dir, EVIDENCE)
    problems: list[str] = []
    if not calc.exists():
        return 0, 0, [f"{skill_dir.name}：找不到 scripts/calc.py"]
    if not ev.exists():
        return 0, 0, [f"{skill_dir.name}：找不到 {EVIDENCE}，無基準可比"]

    cases = as_obj(ev.read_text(encoding="utf-8"))
    if not isinstance(cases, list):
        return 0, 0, [f"{skill_dir.name}：{EVIDENCE} 格式不是陣列"]

    total = 0
    same = 0
    for ci, case in enumerate(cast("list[object]", cases), 1):
        if not isinstance(case, dict):
            continue
        trace = cast("dict[str, object]", case).get("tool_calls")
        if not isinstance(trace, list):
            continue
        for ti, call in enumerate(cast("list[object]", trace), 1):
            if not isinstance(call, dict):
                continue
            c = cast("dict[str, object]", call)
            payload = c.get("payload")
            expected_raw = c.get("result")
            if not isinstance(payload, str) or not isinstance(expected_raw, str):
                continue
            total += 1
            tag = f"{skill_dir.name} 案例{ci} 呼叫{ti}"
            proc = subprocess.run(
                [sys.executable, str(calc)],
                input=payload,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if proc.returncode != 0:
                problems.append(f"{tag}：執行失敗（exit {proc.returncode}）{proc.stderr.strip()[:200]}")
                continue
            try:
                want = as_obj(expected_raw)
                got = as_obj(proc.stdout)
            except ValueError as exc:
                problems.append(f"{tag}：輸出不是合法 JSON（{exc}）")
                continue
            d = diff_keys(want, got)
            if d:
                problems.append(f"{tag}：{len(d)} 處不一致")
                problems.extend(f"    {x}" for x in d[:20])
            else:
                same += 1
    return total, same, problems


def main() -> None:
    args = sys.argv[1:]
    if not args:
        raise SystemExit(__doc__)
    if args[0] == "--all":
        # 使用 rglob 掃描包含 calc.py 的 Skill 目錄。Skill 可位於分區子目錄中；
        # glob("*/scripts/calc.py") 只掃一層，可能漏掉目標並產生假綠燈。
        targets = sorted(
            p.parent.parent
            for root in (BASE / "已完成Skills", BASE / "待驗Skills") if root.is_dir()
            for p in root.rglob("scripts/calc.py")
        )
    else:
        targets = [Path(a) if Path(a).is_absolute() else BASE / a for a in args]

    grand_total = 0
    grand_same = 0
    all_problems: list[str] = []
    for t in targets:
        total, same, problems = run_one(t)
        grand_total += total
        grand_same += same
        all_problems.extend(problems)
        mark = "✅" if total and same == total and not problems else "❌"
        print(f"{mark} {t.name:<40} 一致 {same}/{total}")

    if all_problems:
        print("\n--- 問題明細 ---")
        for p in all_problems:
            print(p)

    print(f"\n總計：比對 {grand_total} 次呼叫，一致 {grand_same}，不一致 {grand_total - grand_same}")
    if grand_same != grand_total or all_problems:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
