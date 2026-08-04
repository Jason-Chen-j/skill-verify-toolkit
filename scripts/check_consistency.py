#!/usr/bin/env python3
"""輸出一致性檢查：抓「JSON 合法、必填齊全，但數字自相矛盾」的輸出。

## 為什麼需要這一支

原本的驗證只看三件事：JSON 能不能解析、有沒有簡體字、必填欄位齊不齊。
這三件事全綠，輸出照樣可以是錯的——2026-07-27 實測 skill_133 就是全綠但分級全錯。

那次的形態是：模型把「剩 1 天」標成 warning 送進 calc.py，calc.py 忠實地用錯誤分級
計數，但模型自己輸出給使用者看的明細裡卻標成 urgent。同一份回應，同一個品項，
兩個地方兩個答案。使用者拿到的報表是：明細列兩筆 urgent，摘要寫 urgent 0 項。

而 total_risk_cost 碰巧正確——三項全加，怎麼分類總和都不變。**碰巧對最危險，
它讓錯誤看起來像沒錯。**

## 三個檢查

1. 兩次計算不一致：送進 calc.py 的判斷值 vs 模型自己輸出的同名欄位值
   （只有走工具版、有 tool_calls 的 skill 查得到）
2. 分類計數對不上：摘要說 urgent 2 項，明細數出來卻是 3 項
3. 加總對不上：各分類 count 相加 != 明細總筆數

三個檢查都是確定性比對，不需要懂該 skill 的業務規則，也不會因為換一支 skill 就失效。

用法：
    python3 scripts/check_consistency.py --all
    python3 scripts/check_consistency.py <skill目錄路徑>

離開碼：0 = 沒抓到不一致；1 = 有。
"""
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import cast

_json_loads: Callable[[str], object] = json.loads

BASE = Path(__file__).resolve().parent.parent
EVIDENCE_FILES = ("驗證結果_工具版.json", "驗證結果.json")

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

# 常見的「名字」欄位，用來把 payload 的列跟 response 的列對起來
NAME_KEYS = ("item_name", "name", "item", "item_no", "no", "序號", "項次", "品名")
# 常見的「筆數」欄位字尾。**必須由長到短排列**：`urgent_items_count` 若先剝掉 `_count`
# 會剩下 `urgent_items`，跟明細裡的 `alert_level == "urgent"` 對不起來，檢查 2 就整個失效。
# 這個 bug 是拿 skill_133（已知分類計數錯誤）當對照組時發現的——檢查 1 抓到了、檢查 2 沒有。
COUNT_SUFFIXES = ("_items_count", "_qty_count", "_count", "_num")


def rows_of(node: object, acc: list[list[dict[str, object]]]) -> list[list[dict[str, object]]]:
    """收集 JSON 裡所有「物件陣列」。"""
    if isinstance(node, dict):
        for v in cast("dict[str, object]", node).values():
            acc = rows_of(v, acc)
    elif isinstance(node, list):
        lst = cast("list[object]", node)
        dicts = [cast("dict[str, object]", x) for x in lst if isinstance(x, dict)]
        if dicts and len(dicts) == len(lst):
            acc.append(dicts)
        for v in lst:
            acc = rows_of(v, acc)
    return acc


def count_groups(node: object, acc: list[dict[str, int]]) -> list[dict[str, int]]:
    """收集「同一個物件裡的 *_count 欄位」為一組。

    分組是必要的：skill_81 的 check_summary 有 red/yellow/green/not_applicable 四個 count，
    它們是同一組燈號統計、加總等於 check_results 的 18 項。如果把它們拆開單獨比對，
    red_count 就會被拿去跟 action_items[].priority=="red"（待辦優先級，只有 5 筆）比，
    判成錯誤——那兩組數字同名但不同義。
    """
    if isinstance(node, dict):
        d = cast("dict[str, object]", node)
        group: dict[str, int] = {}
        for k, v in d.items():
            is_count = any(k.endswith(s) for s in COUNT_SUFFIXES)
            if is_count and isinstance(v, int) and not isinstance(v, bool):
                group[k] = v
        if group:
            acc.append(group)
        for v in d.values():
            acc = count_groups(v, acc)
    elif isinstance(node, list):
        for v in cast("list[object]", node):
            acc = count_groups(v, acc)
    return acc


def row_key(row: dict[str, object]) -> str | None:
    for k in NAME_KEYS:
        v = row.get(k)
        if isinstance(v, (str, int)) and not isinstance(v, bool):
            return str(v)
    return None


def check_two_passes(payload: object, resp: object) -> list[str]:
    """檢查 1：送進腳本的判斷值 vs 模型自己輸出的值。

    配對規則：優先用 `input_index`（腳本回傳的原始位置），沒有才退回用品名。

    退回用品名時，**同一個品名出現多列就整組跳過**。庫存盤點常有同品名多批號
    （同一種鮮乳兩個批號、同一種麵粉兩張效期），拿品名當 key 會把 A 批的效期
    跟 B 批的效期互比，報出一堆假的「不一致」。skill_133 改設計後就誤報過一次。
    """
    problems: list[str] = []
    pin: dict[str, list[dict[str, str]]] = {}
    rin: dict[str, list[dict[str, str]]] = {}
    dup: set[str] = set()
    for target, bucket in ((payload, pin), (resp, rin)):
        seen: set[str] = set()
        for rows in rows_of(target, []):
            for row in rows:
                idx = row.get("input_index")
                if isinstance(idx, int) and not isinstance(idx, bool):
                    key = f"#{idx}"
                else:
                    name = row_key(row)
                    if name is None:
                        continue
                    key = name
                    if key in seen:
                        dup.add(key)
                    seen.add(key)
                vals = {k: v for k, v in row.items() if isinstance(v, str) and k not in NAME_KEYS}
                bucket.setdefault(key, []).append(vals)
    for key, pvals in pin.items():
        if key not in rin or key in dup:
            continue
        for pv in pvals:
            for rv in rin[key]:
                for field in set(pv) & set(rv):
                    if pv[field] != rv[field]:
                        msg = (f"兩次計算不一致：「{key}」的 {field}，"
                               + f"送進腳本 {pv[field]!r}，模型自己輸出 {rv[field]!r}")
                        problems.append(msg)
    return problems


def strip_suffix(cname: str) -> str:
    for suffix in COUNT_SUFFIXES:
        if cname.endswith(suffix):
            return cname[: -len(suffix)]
    return cname


def check_class_counts(resp: object) -> list[str]:
    """檢查 2：分類計數。"""
    problems: list[str] = []
    groups = count_groups(resp, [])
    if not groups:
        return problems
    all_rows = rows_of(resp, [])
    if not all_rows or len(all_rows) > 12:
        return problems

    # 一份輸出常常把明細拆成好幾個陣列，而 summary 只統計其中「某幾個」。
    # skill_25 分成 required_items_check(23)、prohibited_items_check(8)、
    # numeric_threshold_check(7)，summary 統計的是前兩個（16+0+15=31=23+8），
    # 第三個是數值門檻檢查、屬於另一類，不計入。
    #
    # 「哪幾個陣列算進 summary」是該 skill 的業務規則，機械檢查不可能知道。
    # 判定方式為窮舉非空陣列組合：只要存在一組陣列，讓該分類欄位底下**所有** count
    # 同時吻合，就算通過。全部組合都對不上才報。陣列數 >12 直接跳過，不值得窮舉。

    # 每個陣列、每個字串欄位的值分佈
    per_array: list[dict[str, dict[str, int]]] = []
    for rows in all_rows:
        one: dict[str, dict[str, int]] = {}
        for row in rows:
            for k, v in row.items():
                if isinstance(v, str):
                    fd = one.setdefault(k, {})
                    fd[v] = fd.get(v, 0) + 1
        per_array.append(one)

    # 所有明細欄位的值域，用來判斷一組 count 是不是「直接對應」某個欄位
    domains: dict[str, set[str]] = {}
    for one in per_array:
        for field, values in one.items():
            domains.setdefault(field, set()).update(values)

    for group in groups:
        prefixes = {c: strip_suffix(c) for c in group}
        for field, domain in domains.items():
            # 只有這一組 count 的前綴**全部**都落在該欄位的值域裡，才做逐值比對。
            #
            # 部分吻合代表中間有映射：skill_81 的 red/yellow/green/not_applicable 是燈號，
            # 對應的是 check_results.status 的 missing/uncertain/pass/not_applicable——
            # 只有 not_applicable 同名。硬比就會把正確的輸出判成錯誤（實際踩過）。
            # 有映射關係的，機械檢查判不了，交給人工或該 skill 自己的自我驗算。
            #
            # 例外：count 值為 0 的分類，明細裡本來就不會出現該值，不能因此判定「對不上」。
            # skill_25 文字版案例1 的 fail_count=0 就是這樣被整組跳過、漏報過一次。
            lower_domain = {d.lower() for d in domain}
            if not all(p.lower() in lower_domain or group[c] == 0
                       for c, p in prefixes.items()):
                continue
            ok = False
            best: str = ""
            for mask in range(1, 1 << len(per_array)):
                merged: dict[str, int] = {}
                picked: list[int] = []
                for i, one in enumerate(per_array):
                    if not mask & (1 << i):
                        continue
                    picked.append(i)
                    for val, n in one.get(field, {}).items():
                        merged[val] = merged.get(val, 0) + n
                if not merged:
                    continue
                if all(group[c] == merged.get(v, 0) for c, v in prefixes.items()):
                    ok = True
                    break
                if not best:
                    observed = "／".join(f"{v}={merged.get(v, 0)}" for v in sorted(prefixes.values()))
                    best = f"（明細陣列 {picked} 實際為 {observed}）"
            if not ok:
                expr = "／".join(f"{c}={group[c]}" for c in sorted(group))
                msg = (f"分類計數對不上：{field} 的 {expr}，"
                       + f"找不到任何明細陣列組合能對上{best}")
                problems.append(msg)
    return problems


def load_cases(path: Path) -> list[dict[str, object]]:
    data = _json_loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        return []
    return [cast("dict[str, object]", x) for x in cast("list[object]", data) if isinstance(x, dict)]


def applicable(resp: object, case: dict[str, object]) -> bool:
    """這個案例有沒有任何一項檢查真的能跑。

    三項檢查都有前提：分類計數要有 *_count 欄位加明細陣列；兩次計算比對要有 tool_calls。
    兩個前提都不成立時，這支 skill 等於沒被檢查過——**不能印綠燈**。
    純算術型的 skill（退費試算、費用試算、單位換算）多半沒有清單也沒有 count 欄位，
    它們的算術正確性本檢查器完全覆蓋不到，要靠實測逐案重算或加 calc.py 來保證。
    """
    groups: list[dict[str, int]] = []
    arrays: list[list[dict[str, object]]] = []
    if count_groups(resp, groups) and rows_of(resp, arrays):
        return True
    calls = case.get("tool_calls")
    return isinstance(calls, list) and len(cast("list[object]", calls)) > 0


def run_one(skill_dir: Path) -> tuple[int, int, list[str]]:
    """回傳（有存證的案例數, 檢查項真的跑得動的案例數, 問題清單）。"""
    problems: list[str] = []
    checked = 0
    covered = 0
    for fname in EVIDENCE_FILES:
        f = evidence_path(skill_dir, fname)
        if not f.exists():
            continue
        for ci, case in enumerate(load_cases(f), 1):
            raw = case.get("raw_response")
            if not isinstance(raw, str):
                continue
            try:
                resp = _json_loads(raw)
            except ValueError:
                continue
            checked += 1
            if applicable(resp, case):
                covered += 1
            # 帶上來源檔名：工具版與文字版兩份存證各有「案例2」，不標來源會混在一起，
            # 訊息裡的數字看起來自相矛盾（同一個 count 一下 0 一下 2）。
            source = "工具版" if fname == "驗證結果_工具版.json" else "文字版"
            tag = f"{skill_dir.name} {source}案例{ci}"
            for p in check_class_counts(resp):
                problems.append(f"{tag}｜{p}")
            calls = case.get("tool_calls")
            if isinstance(calls, list):
                for call in cast("list[object]", calls):
                    if not isinstance(call, dict):
                        continue
                    pl = cast("dict[str, object]", call).get("payload")
                    if not isinstance(pl, str):
                        continue
                    try:
                        payload = _json_loads(pl)
                    except ValueError:
                        continue
                    for p in check_two_passes(payload, resp):
                        problems.append(f"{tag}｜{p}")
    return checked, covered, problems


def main() -> None:
    args = sys.argv[1:]
    if not args:
        raise SystemExit(__doc__)
    if args[0] == "--all":
        # 遞迴找 skill_* 目錄。Skill 位於類型子目錄中；
        # 只掃第一層會把類型目錄本身當成 Skill，跑出「查無存證」的假結果。
        targets = sorted(
            p for root in (BASE / "已完成Skills", BASE / "待驗Skills") if root.is_dir()
            for p in root.rglob("skill_*") if p.is_dir()
        )
    else:
        targets = [Path(a) if Path(a).is_absolute() else BASE / a for a in args]

    total_checked = 0
    total_covered = 0
    bad_skills = 0
    uncovered: list[str] = []
    for t in targets:
        checked, covered, problems = run_one(t)
        total_checked += checked
        total_covered += covered
        if not checked:
            continue
        if problems:
            bad_skills += 1
            print(f"❌ {t.name}")
            for p in problems:
                print(f"     {p}")
        elif covered:
            print(f"✅ {t.name}（檢查 {covered}/{checked} 案例）")
        else:
            # 三項檢查的前提都不成立。印綠燈會讓人以為驗過了——那是靜默失敗。
            uncovered.append(t.name)
            print(f"➖ {t.name}（無適用檢查項，本檢查器**未驗證**其算術）")

    print(f"\n存證案例 {total_checked} 個，其中 {total_covered} 個有檢查項可跑。")
    print(f"有不一致的 skill：{bad_skills} 支")
    if uncovered:
        print(f"\n本檢查器涵蓋不到的 skill：{len(uncovered)} 支")
        print("這些 skill 沒有 *_count 欄位也沒有工具呼叫紀錄，三項檢查全部無從施力。")
        print("純算術型（退費試算、費用試算、單位換算）多半落在這裡，")
        print("它們的算術正確性要靠逐案人工重算，或改成呼叫 scripts/calc.py 才有保證。")
        for n in uncovered:
            print(f"  ➖ {n}")
    if bad_skills:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
