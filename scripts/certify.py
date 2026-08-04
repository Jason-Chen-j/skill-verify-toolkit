#!/usr/bin/env python3
"""認證（certify）：全套關卡都過，在 skill 資料夾內寫入 certification.json。

三關的最後一關。機測驗結構、實測產存證、judge 判品質，這一支負責「發證」——
把前三關的結果收斂成一個檔案，記錄這支 skill 通過全部關卡與當時的交付檔雜湊。

## 認證關卡（全過才 certified）

  1. 機械檢查    subprocess 跑 check_b_skill.py（一般 skill）或 check_mcp_skill.py（MCP skill），
                 ERROR 必須為 0
  2. 實測存證    存證目錄下 <skill目錄名>_驗證結果.json 必須存在；附 calc.py 的 skill 另須
                 <skill目錄名>_驗證結果_工具版.json
  3. 存證新鮮度  每份存證 mtime 必須 ≥ skill 目錄內全部交付檔的最大 mtime。
                 存證比程式舊，代表講的是上一版
  4. 品質判定    報告目錄群裡找 <skill目錄名>_品質判定.json，驗總計對組成、
                 critical_issues 為空、average_score ≥ 80；判定檔本身也要夠新鮮

任一關沒過就不寫新檔。例外：目錄裡已經有一份 certification.json（代表曾經通過過），
這次沒過就把它改寫成 certified: false（撤銷），不留一份說謊的舊證書在那裡。

## content_hash 演算法（外部要能重算）

取 skill 資料夾內全部檔案，排除 certification.json、.DS_Store、__pycache__/、*.pyc；
按相對路徑（POSIX 分隔）依 Python 字串比較排序——這就是 UTF-8 位元組排序，因為
UTF-8 保留 Unicode code point 的順序，兩種排序法對任何合法字串都會給出同一個結果；
依序把「相對路徑的 UTF-8 位元組 + b"\\x00" + 檔案內容位元組 + b"\\x00"」餵進同一個
SHA-256，輸出 "sha256:" + hexdigest。

## 用法

  python3 certify.py <skill目錄或上層目錄...> [--evidence-dir 目錄]
                     [--report-dir 目錄]... [--dry-run]

  --evidence-dir  存證目錄。沒給時依序找 $SKILL_EVIDENCE_DIR、
                  <根目錄>/測試紀錄/實測存證
  --report-dir    品質判定報告目錄，可重複給多個。沒給時預設 glob
                  <根目錄>/驗證工具包/驗證報告_*/
  --dry-run       全部關卡照跑，印出「會寫入什麼」，但不落任何檔
"""

import argparse
import glob
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import cast

# json.loads 的回傳型別是 Any，會讓型別檢查沿著每一次取值擴散。
# 這裡綁成回傳 object 的 Callable，把不可知的型別鎖在這一行（同 calc_型別範本.py）。
_json_loads: Callable[[str], object] = json.loads

# ── 根目錄與預設路徑（從腳本位置往上找含 已完成Skills 的那一層，
#    不寫死絕對路徑，工具包搬家也不會壞）───────────

def _find_base(scripts_parent: Path) -> Path:
    """從 scripts/ 的上一層（驗證工具包）往上找含 已完成Skills 的那一層。"""
    for cand in (scripts_parent, scripts_parent.parent):
        if (cand / "已完成Skills").is_dir():
            return cand
    return scripts_parent


_HERE = Path(__file__).resolve().parent          # .../驗證工具包/scripts
_BASE = _find_base(_HERE.parent)                 # .../50_AI營運官（找不到就退回 驗證工具包）

DEFAULT_EVIDENCE_DIR = _BASE / "測試紀錄" / "實測存證"
DEFAULT_REPORT_GLOB = str(_BASE / "驗證工具包" / "驗證報告_*")

DATE_SUFFIX_RE = re.compile(r"_(\d{8})$")
EXCLUDE_NAMES = {"certification.json", ".DS_Store"}


# ── 型別窄化 helper（同 judge.py 的邊界收斂寫法）──────────────────────────

def dict_of(node: object) -> dict[str, object] | None:
    if not isinstance(node, dict):
        return None
    return {str(k): v for k, v in cast("dict[object, object]", node).items()}


def as_dict(node: object) -> dict[str, object]:
    return dict_of(node) or {}


def list_of(node: object) -> list[object] | None:
    if not isinstance(node, list):
        return None
    return cast("list[object]", node)


def as_list(node: object) -> list[object]:
    return list_of(node) or []


def is_num(v: object) -> bool:
    """布林值不算數字。True 在 Python 裡等於 1，會安靜地混進加總。"""
    return isinstance(v, (int, float)) and not isinstance(v, bool)


# ── 找 skill 目錄 ─────────────────────────────────────────────────────────

def skill_type(skill_dir: Path) -> str:
    """MCP（含 server.py）／A（scripts/ 內含 .py）／B（其餘）。"""
    if (skill_dir / "server.py").is_file():
        return "MCP"
    scripts_dir = skill_dir / "scripts"
    if scripts_dir.is_dir() and any(scripts_dir.glob("*.py")):
        return "A"
    return "B"


def find_skill_dirs(paths: list[str]) -> list[Path]:
    """同 check_b_skill.find_skill_dirs／check_mcp_skill.find_mcp_dirs 的寫法，
    合併成一支：含 tool.yaml 或 server.py 的目錄就算 skill 目錄，上層目錄自動掃描。
    """
    dirs: set[Path] = set()
    for raw in paths:
        p = Path(raw).resolve()
        if (p / "tool.yaml").is_file() or (p / "server.py").is_file():
            dirs.add(p)
        elif p.is_dir():
            for root, subdirs, files in os.walk(p):
                subdirs[:] = [d for d in subdirs
                              if not d.startswith((".", "_")) and d != "__MACOSX"]
                if "tool.yaml" in files or "server.py" in files:
                    dirs.add(Path(root))
                    subdirs[:] = []
    return sorted(dirs)


# ── content_hash ──────────────────────────────────────────────────────────

def collect_delivery_files(skill_dir: Path) -> list[Path]:
    """skill 目錄內全部交付檔：排除 certification.json／.DS_Store／__pycache__／*.pyc。

    這份清單同時餵給 content_hash 與存證新鮮度比對——會影響雜湊的檔案，改了
    就該讓存證跟著重跑，兩件事本來就是同一組「這一版由什麼組成」，不必分開算兩次。
    """
    out: list[Path] = []
    for p in skill_dir.rglob("*"):
        if not p.is_file():
            continue
        if p.name in EXCLUDE_NAMES or p.suffix == ".pyc":
            continue
        if "__pycache__" in p.parts:
            continue
        out.append(p)
    return out


def content_hash(skill_dir: Path) -> str:
    files = collect_delivery_files(skill_dir)
    files.sort(key=lambda p: p.relative_to(skill_dir).as_posix())
    h = hashlib.sha256()
    for p in files:
        rel = p.relative_to(skill_dir).as_posix()
        h.update(rel.encode("utf-8"))
        h.update(b"\x00")
        h.update(p.read_bytes())
        h.update(b"\x00")
    return "sha256:" + h.hexdigest()


# ── 關卡 1：機械檢查 ───────────────────────────────────────────────────────

def gate_mechanical(skill_dir: Path, stype: str) -> list[str]:
    """subprocess 跑對應的機械檢查腳本，回傳 ERROR 訊息清單（空清單＝過關）。

    --out-dir 指到 tempfile 建的暫存目錄，跑完即釋放；ERROR 明細直接從
    報告 JSON 的 findings 抄出來印，不指向那個會消失的暫存路徑。
    """
    script_name = "check_mcp_skill.py" if stype == "MCP" else "check_b_skill.py"
    report_name = "MCP機械檢查_報告.json" if stype == "MCP" else "機械檢查_報告.json"
    script_path = _HERE / script_name

    with tempfile.TemporaryDirectory(prefix="certify_mech_") as tmp:
        proc = subprocess.run(
            [sys.executable, str(script_path), str(skill_dir), "--out-dir", tmp],
            capture_output=True, text=True, check=False,
        )
        report_path = Path(tmp) / report_name
        if not report_path.is_file():
            stderr = proc.stderr.strip()[:500]
            detail = f"stderr：{stderr}" if stderr else f"returncode={proc.returncode}"
            return [f"[機械檢查] {script_name} 沒有產生報告（{detail}）"]

        try:
            data = as_dict(_json_loads(report_path.read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            return [f"[機械檢查] 報告不是合法 JSON：{exc}"]

        entry = next(
            (as_dict(r) for r in as_list(data.get("results"))
             if as_dict(r).get("dir_name") == skill_dir.name),
            None)
        if entry is None:
            return [f"[機械檢查] 報告裡找不到 {skill_dir.name} 的結果"]

        problems: list[str] = []
        for row in as_list(entry.get("findings")):
            item = list_of(row)
            if item and len(item) >= 3 and item[0] == "ERROR":
                problems.append(f"[機械檢查] {item[1]}：{item[2]}")
        if not problems and entry.get("errors"):
            # errors 計數 > 0 但一項 ERROR 明細都沒解析到：防禦性寫法，
            # 不能因為 findings 格式意外變了就悄悄放行。
            problems.append(f"[機械檢查] ERROR 數為 {entry.get('errors')}，"
                             + "但無法從 findings 解析明細")
        return problems


# ── 關卡 2＋3：實測存證存在／新鮮度 ────────────────────────────────────────

def evidence_file_list(skill_dir: Path, stype: str, evidence_dir: Path) -> list[Path]:
    names = [f"{skill_dir.name}_驗證結果.json"]
    if stype == "A":
        names.append(f"{skill_dir.name}_驗證結果_工具版.json")
    return [evidence_dir / n for n in names]


def newest_delivery_file(skill_dir: Path) -> Path | None:
    files = collect_delivery_files(skill_dir)
    if not files:
        return None
    return max(files, key=lambda p: p.stat().st_mtime)


def gate_evidence(skill_dir: Path, stype: str, evidence_dir: Path) -> list[str]:
    ev_paths = evidence_file_list(skill_dir, stype, evidence_dir)
    missing = [str(p) for p in ev_paths if not p.is_file()]
    if missing:
        return [f"[實測存證] 缺存證：{p}" for p in missing]

    newest = newest_delivery_file(skill_dir)
    if newest is None:
        return []
    problems: list[str] = []
    for ev in ev_paths:
        if ev.stat().st_mtime < newest.stat().st_mtime:
            problems.append(
                f"[存證新鮮度] {ev} 比交付檔 {newest.relative_to(skill_dir)} 舊，判定存證過期")
    return problems


# ── 關卡 4：品質判定 ───────────────────────────────────────────────────────

def pick_quality_file(skill_name: str, report_dirs: list[Path]) -> Path | None:
    """同一支出現在多個報告目錄時的取用規則：目錄名結尾 _YYYYMMDD 日期最新者優先；
    同日者目錄名含「修正後」優先；再同則按目錄名字典序取最後。

    用 (日期, 有無修正後, 目錄名) 這組 key 由小到大排序，取排序後的最後一個，
    三層優先序就是同一個 sort 一次做完，不必分階段各自比較。
    """
    candidates = [d / f"{skill_name}_品質判定.json" for d in report_dirs]
    candidates = [p for p in candidates if p.is_file()]
    if not candidates:
        return None

    def sort_key(p: Path) -> tuple[str, int, str]:
        dirname = p.parent.name
        m = DATE_SUFFIX_RE.search(dirname)
        date = m.group(1) if m else ""
        has_fix = 1 if "修正後" in dirname else 0
        return (date, has_fix, dirname)

    candidates.sort(key=sort_key)
    return candidates[-1]


def check_quality_content(path: Path) -> list[str]:
    """驗三件事：(a) 逐案 total_score 對 scores 加總、average_score 對逐案平均
    （容差 0.01）；(b) critical_issues 為空；(c) average_score ≥ 80。
    """
    try:
        raw = _json_loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [f"{path} 讀取失敗：{exc}"]

    data = dict_of(raw)
    if data is None:
        return [f"{path} 頂層不是物件"]

    problems: list[str] = []
    cases = as_list(data.get("cases"))
    if not cases:
        problems.append(f"{path} 沒有 cases 陣列或為空")

    case_totals: list[float] = []
    for i, case in enumerate(cases, 1):
        c = dict_of(case)
        if c is None:
            problems.append(f"{path} 第 {i} 案不是物件")
            continue
        scores = as_dict(c.get("scores"))
        total = c.get("total_score")
        if not scores or not is_num(total):
            problems.append(f"{path} 第 {i} 案缺 scores 或 total_score")
            continue
        summed = sum(float(cast("float", v)) for v in scores.values() if is_num(v))
        total_f = float(cast("float", total))
        if abs(summed - total_f) > 0.01:
            problems.append(f"{path} 第 {i} 案 total_score={total_f}，但 scores 加總={summed}")
            continue
        case_totals.append(total_f)

    avg = data.get("average_score")
    if not is_num(avg):
        problems.append(f"{path} 缺 average_score")
    else:
        avg_f = float(cast("float", avg))
        if case_totals:
            calc_avg = sum(case_totals) / len(case_totals)
            if abs(calc_avg - avg_f) > 0.01:
                problems.append(f"{path} average_score={avg_f}，但逐案平均={calc_avg:.2f}")
        if avg_f < 80:
            problems.append(f"{path} average_score={avg_f} 未達 80 分門檻")

    crit_list = list_of(data.get("critical_issues"))
    if crit_list is None:
        problems.append(f"{path} critical_issues 不是陣列")
    elif crit_list:
        shown = "；".join(str(x) for x in crit_list[:3])
        problems.append(f"{path} 有 {len(crit_list)} 項嚴重問題：{shown}")

    return problems


def quality_average(skill_name: str, report_dirs: list[Path]) -> float | None:
    """認證檔要記的分數：選中的品質判定檔的 average_score。讀不到就 None。

    分數合不合格由 check_quality_content 把關，這裡只負責「抄下來記進證書」；
    撤銷的證書也要帶當時的分數（可能是 None），讓人看得出是幾分被撤的。
    """
    path = pick_quality_file(skill_name, report_dirs)
    if path is None:
        return None
    try:
        data = dict_of(_json_loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None
    if data is None:
        return None
    avg = data.get("average_score")
    # 取 2 位小數：部分判定檔存的是未取整的平均（97.66666…），證書是對外檔，
    # 統一呈現格式，容差比對仍以判定檔原值為準。
    return round(float(cast("float", avg)), 2) if is_num(avg) else None


def gate_quality(skill_dir: Path, report_dirs: list[Path]) -> list[str]:
    path = pick_quality_file(skill_dir.name, report_dirs)
    if path is None:
        searched = "、".join(str(d) for d in report_dirs) or "（無任何報告目錄）"
        return [f"[品質判定] 缺 {skill_dir.name}_品質判定.json，找過：{searched}"]

    problems = [f"[品質判定] {m}" for m in check_quality_content(path)]
    newest = newest_delivery_file(skill_dir)
    if newest is not None and path.stat().st_mtime < newest.stat().st_mtime:
        problems.append(
            f"[品質判定] {path} 比交付檔 {newest.relative_to(skill_dir)} 舊，判定過期")
    return problems


# ── 發證／撤銷 ─────────────────────────────────────────────────────────────

def evidence_models(skill_dir: Path, stype: str, evidence_dir: Path) -> str | None:
    """認證檔的 tested_model：實測存證記錄的執行模型。

    附 calc.py 的 skill 以工具版存證為準（發證依據的行為證據是它），其餘讀文字版。
    存證沒記錄 model 就回 None——寧可標 null 也不拿別份存證的模型名頂替。
    多案例出現不同模型時全列（+ 接），不挑其中一個。
    """
    name = (f"{skill_dir.name}_驗證結果_工具版.json" if stype == "A"
            else f"{skill_dir.name}_驗證結果.json")
    path = evidence_dir / name
    if not path.is_file():
        return None
    try:
        raw = _json_loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    # 存證的正式格式是頂層陣列（每案一物件、各帶 model）；
    # 早期少數包成 {"results": [...]} 的物件形也吃得下。
    rows = list_of(raw)
    if rows is None:
        d = dict_of(raw)
        rows = as_list(d.get("results")) if d is not None else []
    models: set[str] = set()
    for row in rows:
        r = dict_of(row)
        if r is None:
            continue
        m = r.get("model")
        if isinstance(m, str) and m.strip():
            models.add(m.strip())
    return "+".join(sorted(models)) if models else None


def build_cert(skill_dir: Path, stype: str, certified: bool,
               score: float | None, tested: str | None) -> dict[str, object]:
    return {
        "spec_version": 1,
        "skill_id": skill_dir.name,
        "skill_type": stype,
        "certified": certified,
        "score": score,
        "tested_model": tested,
        "certified_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "content_hash": content_hash(skill_dir),
    }


def atomic_write_cert(skill_dir: Path, cert: dict[str, object]) -> None:
    """先寫暫存檔再 os.replace，避免寫到一半被讀到半份 JSON。"""
    target = skill_dir / "certification.json"
    text = json.dumps(cert, ensure_ascii=False, indent=2) + "\n"
    fd, tmp_path = tempfile.mkstemp(dir=str(skill_dir), prefix=".certification_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            _ = fh.write(text)
        # mkstemp 預設 0600，rename 後權限跟著暫存檔走——證書會變成只有
        # 發證帳號讀得到，同步到別台機器或打包交付時讀不到。統一成 0644。
        os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, target)
    except OSError:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


# ── CLI ────────────────────────────────────────────────────────────────────

def resolve_evidence_dir(cli_value: str | None) -> Path:
    if cli_value:
        return Path(cli_value).resolve()
    env_value = os.environ.get("SKILL_EVIDENCE_DIR")
    if env_value:
        return Path(env_value).resolve()
    return DEFAULT_EVIDENCE_DIR


def resolve_report_dirs(cli_values: list[str]) -> list[Path]:
    if cli_values:
        return [Path(v).resolve() for v in cli_values]
    return sorted(Path(p) for p in glob.glob(DEFAULT_REPORT_GLOB) if os.path.isdir(p))


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    _ = ap.add_argument("paths", nargs="+", help="skill 目錄或上層目錄")
    _ = ap.add_argument("--evidence-dir", default=None)
    _ = ap.add_argument("--report-dir", action="append", default=[])
    _ = ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    skill_dirs = find_skill_dirs(cast("list[str]", args.paths))
    if not skill_dirs:
        print("找不到任何含 tool.yaml 或 server.py 的目錄")
        return 2

    evidence_dir = resolve_evidence_dir(cast("str | None", args.evidence_dir))
    report_dirs = resolve_report_dirs(cast("list[str]", args.report_dir))
    dry_run = cast("bool", args.dry_run)

    n_certified = n_revoked = n_failed = 0

    for skill_dir in skill_dirs:
        stype = skill_type(skill_dir)
        problems: list[str] = []
        problems += gate_mechanical(skill_dir, stype)
        problems += gate_evidence(skill_dir, stype, evidence_dir)
        problems += gate_quality(skill_dir, report_dirs)

        cert_path = skill_dir / "certification.json"
        score = quality_average(skill_dir.name, report_dirs)
        tested = evidence_models(skill_dir, stype, evidence_dir)

        if not problems:
            cert = build_cert(skill_dir, stype, True, score, tested)
            if not dry_run:
                atomic_write_cert(skill_dir, cert)
            print(f"✅ {skill_dir.name}｜{stype}｜certified")
            if dry_run:
                print(f"    會寫入 {cert_path}：")
                for line in json.dumps(cert, ensure_ascii=False, indent=2).splitlines():
                    print(f"    {line}")
            n_certified += 1
            continue

        if cert_path.is_file():
            cert = build_cert(skill_dir, stype, False, score, tested)
            if not dry_run:
                atomic_write_cert(skill_dir, cert)
            print(f"↩️  {skill_dir.name}｜{stype}｜撤銷（原有 certification.json 改為 certified: false）")
            if dry_run:
                print(f"    會寫入 {cert_path}：")
                for line in json.dumps(cert, ensure_ascii=False, indent=2).splitlines():
                    print(f"    {line}")
            n_revoked += 1
        else:
            print(f"❌ {skill_dir.name}｜{stype}｜未通過（不寫入 certification.json）")
        for p in problems:
            print(f"    {p}")
        n_failed += 1

    print()
    print(f"總結：{len(skill_dirs)} 支｜通過 {n_certified}｜沒過 {n_failed}（其中撤銷 {n_revoked}）")
    return 1 if n_failed else 0


if __name__ == "__main__":
    sys.exit(main())
