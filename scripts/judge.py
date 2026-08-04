#!/usr/bin/env python3
"""判定（judge）：吃實測存證，逐案判它過不過。

三關的第三關。機測驗結構、實測產存證，這一支負責「判」。

## 為什麼需要這一支

實測只判三件事：JSON 可解析、必填欄位齊全、輸出語言。這三件全過，
輸出裡的數字仍然可能是錯的——總分對不上明細、字數欄位對不上實際字數、
單項分數超過滿分。這些缺陷不會讓 JSON 壞掉，所以三件小事全部放行。

實際案例：一支評分型 skill 三項小檢查全過，逐列重算後 30 列裡 9 列的
`score_total` 對不上 `score_breakdown` 七項之和。它一路被歸進
「無適用檢查項」，整批驗證都沒發現。

## 判什麼

八項，全部通用，不寫死任何一支 skill 的欄位名：

  J1 解析診斷      空輸出／markdown 圍欄／截斷／**輸出了不只一份 JSON**
  J2 schema 驗證   type、enum、範圍、additionalProperties，不只必填欄位
  J3 總計對組成    `*_total` 對不對得上同層的明細加總
  J4 分類計數      `*_count` 對不對得上明細筆數（沿用 check_consistency 的窮舉法）
  J5 轉錄忠實度    有 calc.py 時，腳本回傳值有沒有被原封不動抄進最終輸出
  J6 輸出語言      只在 SKILL.md 自己宣告「一律繁體中文」時才判。**平台可以選擇
                   回答語系**，skill 沒宣告輸出語言的話，出現簡體字不算缺陷
  J7 存證誠信      model 欄位、案例數、raw_response 是否非空
  J8 存證新鮮度    存證比 SKILL.md／腳本舊，代表講的是上一版

## 判定分級

  ❌ 不通過            機械八項任一查到問題，或品質判定為不通過
  ➖ 待補驗            查無實測存證
  ➖ 品質未判定        機械八項無問題，但還沒有 <skill>_品質判定.json
  ➖ 需修正            品質 60–79 分
  ⚠️ 通過，有可忍受問題  品質 80–89 分
  ✅ 可直接交付        品質 90–100 分且無嚴重問題

「待補驗」「品質未判定」不是有問題，是還沒被驗到那一層。每支都會印
「驗了什麼、沒驗什麼」，沒驗到的那些是盲區清單，要當待辦，不是當結案。

## 腳本判不了的交給模型

法規引用是否標齊、日期起算點對不對、禁止行為有沒有違反——腳本判不了。
規則在本檔的 RUBRIC 段：品質判定模型照它逐案判，寫出 <skill>_品質判定.json；
本腳本驗證該檔的加總、平均與結論一致，並把結果併進最終判定。

用法：
  python3 scripts/judge.py <skill目錄或上層目錄...> [--evidence-dir 目錄] [--out-dir 目錄]
  python3 scripts/judge.py --selftest    跑自身對照組，確認判定器真的抓得到
"""

import argparse
import json
import os
import re
import subprocess
import sys
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import cast

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

# 同目錄的姊妹腳本。這幾支是被當腳本跑的，不會被當套件 import，
# 所以隱含相對匯入在這裡是刻意的；型別檢查的提醒逐行標明原因。
import check_b_skill  # pyright: ignore[reportImplicitRelativeImport]
import check_consistency  # pyright: ignore[reportImplicitRelativeImport]

_json_loads: Callable[[str], object] = json.loads

# 存證檔在受測 skill 目錄底下時用這兩個名字；在報告目錄底下時是 <skill目錄名>_驗證結果.json
EVIDENCE_IN_SKILL = ("驗證結果_工具版.json", "驗證結果.json")

# 這些字出現在欄位名裡，代表它是「上限／額度」不是「實際加總」。
# 同名不同義是判定器最常見的誤判來源：欄位都叫 total，語意是兩回事。
NOT_A_SUM = re.compile(r"ceiling|limit|max|cap|quota|budget|上限|額度|預算|門檻")

# 明細物件裡出現這類欄位，代表它是「算式紀錄」（數量 × 單價 = 小計）
# 而不是「加項清單」。把數量跟單價加進金額裡毫無意義，一律不比。
NOT_AN_ADDEND = re.compile(
    r"rate|ratio|percent|pct|unit|qty|quantity|count|days|nights|hours|price|per_"
    + r"|單價|數量|天數|日數|時數|費率|比率|折扣|倍數")

DIMENSIONS = (
    ("J1", "JSON 格式"),
    ("J2", "輸出 Schema"),
    ("J3", "總計"),
    ("J4", "項目數量"),
    ("J5", "計算腳本輸出"),
    ("J6", "輸出語言"),
    ("J7", "存證資料"),
    ("J8", "存證時間"),
)
QUALITY_DIMENSIONS = (
    "任務完成度", "輸入忠實度", "規格與風險遵守", "可直接使用性", "語言與表達",
)
QUALITY_OUTCOMES = (
    "可直接交付", "通過，有可忍受問題", "需修正", "不通過",
)
MARK = {
    "可直接交付": "✅",
    "通過，有可忍受問題": "⚠️",
    "需修正": "➖",
    "待補驗": "➖",
    "品質未判定": "➖",
    "不通過": "❌",
}


# ── 姊妹腳本的邊界 ────────────────────────────────────────────────────
# 那幾支沒有型別標註，回傳型別是 Unknown。全部包在這裡，
# 不讓未知型別擴散到下面每一行。

# check_b_skill.py 沒有型別標註，取到的函式型別是未知。全部在這裡綁成有型別的
# Callable，未知就鎖在這五行，不會沿著呼叫端擴散。
_find_dirs = cast("Callable[[list[str]], list[str]]", check_b_skill.find_skill_dirs)
_scan_simplified = cast("Callable[[str], list[tuple[int, str, str]]]",
                        check_b_skill.scan_simplified)
_lang_re: re.Pattern[str] = check_b_skill.RULE_LANGUAGE
_class_counts: Callable[[object], list[str]] = check_consistency.check_class_counts
_count_groups: Callable[[object, list[dict[str, int]]], list[dict[str, int]]] = (
    check_consistency.count_groups)


def find_skill_dirs(paths: list[str]) -> list[str]:
    return _find_dirs(paths)


def simplified_chars(text: str) -> list[str]:
    """回傳文字裡出現的簡體字（去重排序）。"""
    return sorted({h[1] for h in _scan_simplified(text)})


def declares_zh_hant(text: str) -> bool:
    return bool(_lang_re.search(text))


def count_problems(resp: object) -> list[str]:
    return _class_counts(resp)


def has_count_fields(resp: object) -> bool:
    return bool(_count_groups(resp, []))


# ── 共用 ──────────────────────────────────────────────────────────────
# dict_of／list_of 回傳 None 而不是空容器，是為了讓呼叫端用回傳值決定流程，
# 不要在呼叫前先 isinstance 窄化——先窄化再傳進 object 參數，型別檢查會一路報
# 「引數型別部分未知」，而且那個未知會沿著每一次取值擴散。

def dict_of(node: object) -> dict[str, object] | None:
    if not isinstance(node, dict):
        return None
    return {str(k): v for k, v in cast("dict[object, object]", node).items()}


def list_of(node: object) -> list[object] | None:
    if not isinstance(node, list):
        return None
    return cast("list[object]", node)


def as_dict(node: object) -> dict[str, object]:
    return dict_of(node) or {}


def as_list(node: object) -> list[object]:
    return list_of(node) or []


def dicts_in(node: object) -> list[dict[str, object]]:
    """把陣列裡的物件挑出來，非物件的元素跳過。"""
    out: list[dict[str, object]] = []
    for item in as_list(node):
        d = dict_of(item)
        if d is not None:
            out.append(d)
    return out


def is_num(v: object) -> bool:
    """布林值不算數字。True 在 Python 裡等於 1，會安靜地混進加總。"""
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def walk_dicts(node: object, acc: list[tuple[str, dict[str, object]]] | None = None,
               path: str = "$") -> list[tuple[str, dict[str, object]]]:
    """深度走訪，收集每一個物件與它的路徑。"""
    out: list[tuple[str, dict[str, object]]] = [] if acc is None else acc
    d = dict_of(node)
    if d is not None:
        out.append((path, d))
        for k, v in d.items():
            _ = walk_dicts(v, out, f"{path}.{k}")
        return out
    rows = list_of(node)
    if rows is not None:
        for i, v in enumerate(rows):
            _ = walk_dicts(v, out, f"{path}[{i}]")
    return out


# ── J1 解析診斷 ───────────────────────────────────────────────────────

def parse_diagnosis(raw: object) -> tuple[object | None, str]:
    """解析輸出並說清楚壞在哪。回傳（物件或 None, 問題描述；空字串代表乾淨）。

    `parse_ok: false` 只說「壞了」，不說壞在哪。截斷、多印一份、忘了拿掉
    markdown 圍欄，三種原因的處理方式完全不同。
    """
    s = raw.strip() if isinstance(raw, str) else ""
    if not s:
        return None, "輸出是空的"

    note = ""
    if s.startswith("```"):
        note = "輸出帶 markdown 圍欄，平台拿到的不是純 JSON"
        s = re.sub(r"^```[A-Za-z]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s.rstrip())

    try:
        obj, end = cast("tuple[object, int]", json.JSONDecoder().raw_decode(s))
    except json.JSONDecodeError as exc:
        tail = "（尾端未閉合，像是被截斷）" if not s.rstrip().endswith(("}", "]")) else ""
        return None, f"不是合法 JSON：{exc}{tail}"

    rest = s[end:].strip()
    if rest:
        try:
            _ = cast("tuple[object, int]", json.JSONDecoder().raw_decode(rest))
            dup = "輸出了不只一份 JSON（同一份答案印了兩次），平台會 parse 失敗"
            return obj, (note + "；" + dup) if note else dup
        except json.JSONDecodeError:
            noise = f"JSON 之後還有 {len(rest)} 個字元的雜訊"
            return obj, (note + "；" + noise) if note else noise
    return obj, note


# ── J2 schema 驗證 ────────────────────────────────────────────────────

def type_ok(node: object, name: str) -> bool:
    """JSON Schema 的 type 判定。integer 不收布林值。"""
    if name == "object":
        return isinstance(node, dict)
    if name == "array":
        return isinstance(node, list)
    if name == "string":
        return isinstance(node, str)
    if name == "boolean":
        return isinstance(node, bool)
    if name == "null":
        return node is None
    if name == "number":
        return is_num(node)
    if name == "integer":
        return isinstance(node, int) and not isinstance(node, bool)
    return True  # schema 用了本驗證器不認得的 type，不判


def validate(node: object, schema: object, path: str = "$") -> list[str]:
    """JSON Schema 子集驗證：type／required／properties／additionalProperties／
    items／enum／const／minimum／maximum／minItems／maxItems／minLength／maxLength。

    只驗 schema 明文寫的規則，不自行加碼。schema 沒宣告的一律不判。
    """
    sc = as_dict(schema)
    if not sc:
        return []
    out: list[str] = []

    raw_types = sc.get("type")
    declared = list_of(raw_types)
    names = [t for t in (declared if declared is not None else [raw_types])
             if isinstance(t, str)]
    if names and not any(type_ok(node, t) for t in names):
        return [f"{path} 型別應為 {'／'.join(names)}，實際是 {type(node).__name__}"]

    if "enum" in sc:
        allowed = as_list(sc["enum"])
        if allowed and node not in allowed:
            out.append(f"{path} 不在 enum 允許值內：{node!r}")
    if "const" in sc and node != sc["const"]:
        out.append(f"{path} 應為常數 {sc['const']!r}，實際 {node!r}")

    if is_num(node):
        n = cast("float", node)
        lo, hi = sc.get("minimum"), sc.get("maximum")
        if is_num(lo) and n < cast("float", lo):
            out.append(f"{path} = {node} 小於下限 {lo}")
        if is_num(hi) and n > cast("float", hi):
            out.append(f"{path} = {node} 超過上限 {hi}")

    if isinstance(node, str):
        lo, hi = sc.get("minLength"), sc.get("maxLength")
        if isinstance(lo, int) and len(node) < lo:
            out.append(f"{path} 長度 {len(node)} 短於規定的 {lo}")
        if isinstance(hi, int) and len(node) > hi:
            out.append(f"{path} 長度 {len(node)} 長於規定的 {hi}")

    d = dict_of(node)
    if d is not None:
        props = as_dict(sc.get("properties"))
        for key in as_list(sc.get("required")):
            if isinstance(key, str) and key not in d:
                out.append(f"{path} 缺必填欄位 {key}")
        if sc.get("additionalProperties") is False:
            for extra in sorted(set(d) - set(props)):
                out.append(f"{path} 多了 schema 未宣告的欄位 {extra}")
        for key, sub in props.items():
            if key in d:
                out += validate(d[key], sub, f"{path}.{key}")

    items = list_of(node)
    if items is not None:
        lo, hi = sc.get("minItems"), sc.get("maxItems")
        if isinstance(lo, int) and len(items) < lo:
            out.append(f"{path} 有 {len(items)} 筆，少於規定的 {lo}")
        if isinstance(hi, int) and len(items) > hi:
            out.append(f"{path} 有 {len(items)} 筆，多於規定的 {hi}")
        sub = sc.get("items")
        if sub is not None:
            for i, v in enumerate(items):
                out += validate(v, sub, f"{path}[{i}]")
    return out


# ── J3 總計對組成 ─────────────────────────────────────────────────────

TOTAL_WORDS = ("total", "sum", "總計", "合計")


def is_total_key(low: str) -> bool:
    return low.endswith("_total") or low.startswith("total") or low in TOTAL_WORDS


def check_totals(resp: object) -> tuple[list[str], int]:
    """`*_total` 對不對得上同層明細加總。回傳（問題清單, 實際比對了幾組）。

    只比兩種明確的組合，比不出來就不比——寧可漏報也不誤報：
      (a) 同層有一個純數字物件，欄位數 ≥ 2，**而且名字對得上** → 拿它的和比
      (b) 同層有一個物件陣列，每筆都有跟 total 去掉前後綴同名的數字欄位 → 拿它的和比

    名字要對得上這一條不能省。同一層常常有好幾組數字，不設這道門，
    `score_total` 會被拿去跟 `cost_detail` 比，判出一個根本不存在的缺陷。
    名字對不上就不比，並在報告裡列為「沒驗到」——那是誠實的漏報，不是綠燈。

    名字含「上限／額度／預算」的欄位一律跳過：那是規則上限，不是實際加總。
    """
    problems: list[str] = []
    compared = 0
    for path, d in walk_dicts(resp, []):
        for key, val in d.items():
            low = key.lower()
            if not is_num(val) or not is_total_key(low) or NOT_A_SUM.search(low):
                continue
            total = cast("float", val)
            stem = re.sub(r"^total_?|_?total$", "", low).strip("_")

            # (a) 同層的純數字物件
            maybe = [(k2, dict_of(v2)) for k2, v2 in d.items() if k2 != key]
            pairs = [(k2, inner) for k2, inner in maybe
                     if inner is not None and len(inner) >= 2
                     and all(is_num(x) for x in inner.values())
                     and not any(NOT_AN_ADDEND.search(k3.lower()) for k3 in inner)]
            if stem:
                pairs = [p for p in pairs if stem in p[0].lower()]
            elif len(pairs) != 1:
                # 欄位名就叫 total／合計，沒有詞根可對。同層有兩組以上明細時
                # 無從判斷該比哪一組，一律不比。
                pairs = []
            for k2, inner in pairs:
                compared += 1
                got = sum(cast("float", x) for x in inner.values())
                if round(got - total, 6) != 0:
                    detail = " + ".join(f"{k}={v}" for k, v in inner.items())
                    problems.append(
                        f"{path}.{key} = {val}，但 {k2} 的 {len(inner)} 項加總是 {got}"
                        + f"（{detail}）")

            # (b) 同層的物件陣列，欄位名與 total 去掉前後綴後相同
            if not stem:
                continue
            for k2, v2 in d.items():
                rows = dicts_in(v2)
                if len(rows) < 2 or not all(is_num(r.get(stem)) for r in rows):
                    continue
                compared += 1
                got = sum(cast("float", r[stem]) for r in rows)
                if round(got - total, 6) != 0:
                    problems.append(
                        f"{path}.{key} = {val}，但 {k2}[].{stem} 的 {len(rows)} 筆"
                        + f"加總是 {got}")
    return problems, compared


# ── J5 轉錄忠實度 ─────────────────────────────────────────────────────

def leaf_numbers(node: object,
                 acc: dict[str, set[float]] | None = None) -> dict[str, set[float]]:
    """把巢狀結構壓成「欄位名 → 出現過的數值集合」。

    只收數字，不收字串：列舉值被改寫成人話（`market_default` → 「未指定，採市場慣例」、
    `含稅` → 「含稅金額」）是正常的，那不是腳本回傳值被竄改。腳本存在的理由是數字。

    用集合不用多重集合：腳本常在自己的輸出裡把同一個值放兩次
    （例如同時放在頂層與比較區塊），模型只寫一次。值一樣就不算缺漏。
    """
    out: dict[str, set[float]] = {} if acc is None else acc
    d = dict_of(node)
    if d is not None:
        for k, v in d.items():
            if dict_of(v) is not None or list_of(v) is not None:
                _ = leaf_numbers(v, out)
            elif is_num(v):
                out.setdefault(k, set()).add(float(cast("float", v)))
        return out
    rows = list_of(node)
    if rows is not None:
        for v in rows:
            _ = leaf_numbers(v, out)
    return out


def check_transcription(case: dict[str, object], resp: object) -> tuple[list[str], int]:
    """腳本回傳值有沒有被原封不動抄進最終輸出。

    有呼叫腳本不等於有照抄。模型可能呼叫了腳本，卻用自己心算的數字填最終 JSON。

    要同時滿足三個條件才算缺漏，缺一不可：
      1. 這個欄位名在腳本回傳與最終輸出兩邊都有（只有一邊有代表模型改了結構，無從判斷）
      2. 兩邊在這個欄位上的數值完全沒有交集
      3. 腳本算的那個數字在**整份輸出裡任何地方都找不到**

    第 3 條不能省。模型常把腳本某個分支的數字搬到別的欄位名底下
    （例如自用住宅稅額填進 `tier_1_tax`），數字本身沒被改，只是換了位置。
    只看欄位名就報缺漏，會把「換位置」誤判成「改數字」。

    腳本自己的稽核欄位（規則說明、候選總數）因為條件 1 不成立，不會被誤判成缺漏。
    """
    calls = dicts_in(case.get("tool_calls"))
    if not calls:
        return [], 0
    out_nums = leaf_numbers(resp)
    anywhere = {v for vs in out_nums.values() for v in vs}
    problems: list[str] = []
    compared = 0
    for i, call in enumerate(calls, 1):
        result = call.get("result")
        if isinstance(result, str):
            try:
                result = _json_loads(result)
            except json.JSONDecodeError:
                problems.append(f"第 {i} 次腳本呼叫的回傳不是合法 JSON")
                continue
        for key, want in leaf_numbers(result).items():
            got = out_nums.get(key)
            if got is None:
                continue
            compared += 1
            missing = sorted(want - got - anywhere)
            if missing:
                shown = "、".join(f"{v:g}" for v in missing[:4])
                here = "、".join(f"{v:g}" for v in sorted(got)[:4])
                problems.append(
                    f"欄位 {key}：腳本算出來的 {shown} 在整份輸出裡都找不到"
                    + f"（該欄位寫的是 {here}）")
    return problems, compared


# ── J8 存證新鮮度 ─────────────────────────────────────────────────────

def stale_against(skill_dir: str, evidence_path: str) -> str:
    """存證比產出物舊就回一句話。存證的意義是「這一版跑出來長這樣」，
    改完沒重跑，存證講的就是上一版的事。"""
    srcs = [os.path.join(skill_dir, "SKILL.md")]
    sdir = os.path.join(skill_dir, "scripts")
    if os.path.isdir(sdir):
        srcs += [os.path.join(sdir, f) for f in sorted(os.listdir(sdir))
                 if f.endswith((".py", ".php"))]
    srcs = [p for p in srcs if os.path.isfile(p)]
    if not srcs or not os.path.isfile(evidence_path):
        return ""
    newest = max(os.path.getmtime(p) for p in srcs)
    if os.path.getmtime(evidence_path) < newest:
        who = os.path.basename(max(srcs, key=os.path.getmtime))
        return f"存證比 {who} 舊，講的是上一版；重跑實測再判"
    return ""


# ── 單支 skill 的判定 ─────────────────────────────────────────────────

@dataclass
class Record:
    skill: str
    dir: str
    verdict: str = "待補驗"
    cases: int = 0
    evidence: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    exercised: list[str] = field(default_factory=list)
    idle: list[str] = field(default_factory=list)
    note_language: str = ""
    note_blind: str = ""
    quality_file: str = ""
    quality_score: float | None = None
    quality_verdict: str = "未判定"
    quality_summary: str = ""
    quality_findings: list[str] = field(default_factory=list)
    quality_critical_issues: list[str] = field(default_factory=list)
    quality_tolerable_issues: list[str] = field(default_factory=list)


def find_evidence(skill_dir: str, evidence_dir: str | None) -> list[str]:
    """存證可能在 skill 目錄底下，也可能在報告目錄底下，兩種都找。"""
    found = [os.path.join(skill_dir, n) for n in EVIDENCE_IN_SKILL
             if os.path.isfile(os.path.join(skill_dir, n))]
    if evidence_dir and os.path.isdir(evidence_dir):
        name = os.path.basename(skill_dir.rstrip("/"))
        for suffix in ("_驗證結果_工具版.json", "_驗證結果.json"):
            p = os.path.join(evidence_dir, name + suffix)
            if os.path.isfile(p):
                found.append(p)
    return found


def read_json(path: str) -> object:
    with open(path, encoding="utf-8") as f:
        return _json_loads(f.read())


def read_text(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def quality_outcome(score: float, critical_issues: list[str]) -> str:
    """嚴重問題優先於分數；分數只判可忍受問題與整體品質。"""
    if critical_issues or score < 60:
        return "不通過"
    if score < 80:
        return "需修正"
    if score < 90:
        return "通過，有可忍受問題"
    return "可直接交付"


def string_list(value: object) -> list[str] | None:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return None
    return cast("list[str]", value)


def load_quality_judgment(rec: Record, evidence_dir: str | None) -> None:
    """讀取模型品質判定，並驗證嚴重問題、分數與結論的一致性。"""
    if not evidence_dir:
        return
    path = os.path.join(evidence_dir, f"{rec.skill}_品質判定.json")
    if not os.path.isfile(path):
        return
    rec.quality_file = os.path.basename(path)
    # 機械檢查已記錄的問題（例如 schema 不符）不應讓內容品質分數消失。
    # 後續只在品質判定檔本身新增問題時停止匯入分數；最終 verdict 仍由 rec.problems 判為不通過。
    problems_before_quality = len(rec.problems)
    try:
        data = as_dict(read_json(path))
    except (OSError, json.JSONDecodeError) as exc:
        rec.problems.append(f"[品質判定] 檔案讀不到或不是合法 JSON：{exc}")
        return
    if data.get("version") != 2:
        rec.problems.append("[品質判定] 需要 version: 2 的 100 分判定資料")
        return
    if data.get("skill") != rec.skill:
        rec.problems.append("[品質判定] skill 名稱與存證目錄不一致")
        return
    if data.get("criteria") != list(QUALITY_DIMENSIONS):
        rec.problems.append("[品質判定] criteria 必須是固定的五個 20 分維度")
        return
    verdict = data.get("verdict")
    if verdict not in QUALITY_OUTCOMES:
        rec.problems.append("[品質判定] verdict 不符合 100 分判定規則")
        return
    score = data.get("average_score")
    if not is_num(score) or not 0 <= score <= 100:
        rec.problems.append("[品質判定] average_score 必須是 0 到 100 的數字")
        return
    critical_issues = string_list(data.get("critical_issues"))
    tolerable_issues = string_list(data.get("tolerable_issues"))
    if critical_issues is None or tolerable_issues is None:
        rec.problems.append("[品質判定] critical_issues 與 tolerable_issues 必須是文字陣列")
        return
    cases = dicts_in(data.get("cases"))
    if not cases:
        rec.problems.append("[品質判定] 沒有逐案結果")
        return
    if rec.cases and len(cases) != rec.cases:
        rec.problems.append(f"[品質判定] 判了 {len(cases)} 案，但存證有 {rec.cases} 案")
        return
    case_totals: list[float] = []
    summary = data.get("summary")
    rec.quality_summary = summary if isinstance(summary, str) else ""
    for i, case in enumerate(cases, 1):
        scores = as_dict(case.get("scores"))
        if set(scores) != set(QUALITY_DIMENSIONS):
            rec.problems.append(f"[品質判定 案例{i}] scores 必須包含五個固定維度")
            continue
        if not all(is_num(scores[key]) and 0 <= scores[key] <= 20
                   for key in QUALITY_DIMENSIONS):
            rec.problems.append(f"[品質判定 案例{i}] 每個維度必須是 0 到 20 分")
            continue
        total = case.get("total_score")
        calculated_total = sum(float(cast("int | float", scores[key]))
                               for key in QUALITY_DIMENSIONS)
        if not is_num(total) or abs(float(total) - calculated_total) > 0.01:
            rec.problems.append(f"[品質判定 案例{i}] total_score 與五個維度加總不一致")
            continue
        case_critical = string_list(case.get("critical_issues"))
        case_tolerable = string_list(case.get("tolerable_issues"))
        findings = string_list(case.get("findings"))
        if case_critical is None or case_tolerable is None or findings is None:
            rec.problems.append(f"[品質判定 案例{i}] findings 必須是文字陣列")
            continue
        case_totals.append(float(total))
        rec.quality_critical_issues += [f"案例{i}：{item}" for item in case_critical]
        rec.quality_tolerable_issues += [f"案例{i}：{item}" for item in case_tolerable]
        rec.quality_findings += [f"案例{i}：{item}" for item in findings]

    if len(rec.problems) > problems_before_quality:
        return
    calculated_average = sum(case_totals) / len(case_totals)
    if abs(float(score) - calculated_average) > 0.1:
        rec.problems.append("[品質判定] average_score 與逐案平均不一致")
        return
    all_critical = critical_issues + rec.quality_critical_issues
    expected = quality_outcome(float(score), all_critical)
    if verdict != expected:
        rec.problems.append(f"[品質判定] 分數與嚴重問題應判為「{expected}」，實際為「{verdict}」")
        return
    rec.quality_score = float(score)
    rec.quality_verdict = cast("str", verdict)
    rec.quality_critical_issues = all_critical
    rec.quality_tolerable_issues = tolerable_issues + rec.quality_tolerable_issues


def set_verdict(rec: Record) -> None:
    """合併機械檢查與模型品質判定，不以不適用的計算檢查阻擋純生成 Skill。"""
    if rec.problems:
        rec.verdict = "不通過"
        return
    if rec.quality_verdict in QUALITY_OUTCOMES:
        rec.verdict = rec.quality_verdict
        return
    rec.verdict = "品質未判定"
    rec.note_blind = "內容品質判定需要 <skill>_品質判定.json。"


def judge_skill(skill_dir: str, evidence_dir: str | None) -> Record:
    rec = Record(skill=os.path.basename(skill_dir.rstrip("/")), dir=skill_dir)

    files = find_evidence(skill_dir, evidence_dir)
    rec.evidence = [os.path.basename(p) for p in files]
    if not files:
        rec.idle = [f"{c} {n}" for c, n in DIMENSIONS]
        rec.problems.append("查無實測存證")
        return rec

    schema: object = None
    sp = os.path.join(skill_dir, "schemas", "response.json")
    if os.path.isfile(sp):
        try:
            schema = read_json(sp)
        except (OSError, json.JSONDecodeError):
            rec.problems.append("schemas/response.json 讀不到或不是合法 JSON")

    n_examples = 0
    ep = os.path.join(skill_dir, "examples.json")
    if os.path.isfile(ep):
        try:
            n_examples = len(as_list(read_json(ep)))
        except (OSError, json.JSONDecodeError):
            pass

    # 這支 skill 有沒有自己宣告「一律繁體中文輸出」。沒宣告就不判語系——
    # 平台可以選擇回答語系，判定器無權拿自己的偏好當標準。
    mp = os.path.join(skill_dir, "SKILL.md")
    zh_hant = declares_zh_hant(read_text(mp)) if os.path.isfile(mp) else False
    if not zh_hant:
        rec.note_language = "SKILL.md 未宣告輸出語言，語系由平台決定，本次不判語系"

    hits = dict.fromkeys((c for c, _ in DIMENSIONS), 0)

    for path in files:
        tag = os.path.basename(path)
        stale = stale_against(skill_dir, path)
        if stale:
            rec.problems.append(f"[J8 {tag}] {stale}")
        hits["J8"] += 1

        try:
            cases = dicts_in(read_json(path))
        except (OSError, json.JSONDecodeError) as exc:
            rec.problems.append(f"[J7 {tag}] 存證檔讀不到或不是合法 JSON：{exc}")
            continue

        hits["J7"] += 1
        if not cases:
            rec.problems.append(f"[J7 {tag}] 存證是空的")
            continue
        if n_examples and len(cases) != n_examples:
            rec.problems.append(
                f"[J7 {tag}] 存證 {len(cases)} 案，examples.json 有 {n_examples} 組"
                + "——少測的那幾組等於沒驗")
        rec.cases += len(cases)

        for i, case in enumerate(cases, 1):
            where = f"{tag} 案例{i}"
            model = case.get("model")
            bad_model = (not isinstance(model, str) or not model.strip()
                         or model.strip().lower() in ("unknown", "n/a", "none", "模型"))
            if bad_model:
                rec.problems.append(f"[J7 {where}] model 欄位沒填實際模型名")

            resp, why = parse_diagnosis(case.get("raw_response"))
            hits["J1"] += 1
            if why:
                rec.problems.append(f"[J1 {where}] {why}")
            if resp is None:
                continue

            if schema is not None:
                hits["J2"] += 1
                for msg in validate(resp, schema)[:8]:
                    rec.problems.append(f"[J2 {where}] {msg}")

            tp, n_total = check_totals(resp)
            hits["J3"] += n_total
            rec.problems += [f"[J3 {where}] {m}" for m in tp]

            if has_count_fields(resp):
                hits["J4"] += 1
            rec.problems += [f"[J4 {where}] {m}" for m in count_problems(resp)]

            tr, n_tr = check_transcription(case, resp)
            hits["J5"] += n_tr
            rec.problems += [f"[J5 {where}] {m}" for m in tr]

            # J6 判的是「有沒有違反 skill 自己宣告的輸出語言」，不是「有沒有簡體字」。
            if zh_hant:
                hits["J6"] += 1
                chars = simplified_chars(json.dumps(resp, ensure_ascii=False))
                if chars:
                    rec.problems.append(
                        f"[J6 {where}] SKILL.md 宣告一律繁體中文輸出，實際出現簡體字："
                        + "".join(chars))

    for code, label in DIMENSIONS:
        (rec.exercised if hits[code] else rec.idle).append(f"{code} {label}")
    load_quality_judgment(rec, evidence_dir)
    set_verdict(rec)
    return rec


# ── 品質判定規則（交給模型判的部分）────────────────────────────────────

RUBRIC = """# 模型品質判定規則

讀取 SKILL.md、examples.json、案例 input 與 raw_response。只根據可核對的資料判定。

## 嚴重問題

下列問題列入 `critical_issues`，不論分數一律判定「不通過」：

- 金額、利息、退款、費率或其他影響金錢的計算錯誤。
- 法規、期限、資格條件或日期錯誤。日期驗兩件：起算點（事件當日或翌日）
  與期間長度——起算點錯，算術對答案也是錯的，屬此類。
- 法規引用未標齊五件：法規全名、條號（含項、款）、最後修正日期、
  生效狀態（已施行／尚未施行）、條文原文。缺任一件即屬此類。
- 當事人名稱、身分、地址、標的或使用者提供的事實被改寫。
- 漏掉安全限制，或違反 SKILL.md 的禁止行為。

## 可忍受問題

字數或頁數估算、語氣、段落順序與可讀性問題列入 `tolerable_issues`。這些問題扣分，
但不單獨造成不通過。

## 規格對照

skill 內建參考值（費率、級距、給付標準）要有三層：覆寫欄位、版本日期、揭露警語。
缺層記進 `tolerable_issues`，在「規格與風險遵守」維度扣分。

## 分數

五個維度各 0 至 20 分：任務完成度、輸入忠實度、規格與風險遵守、可直接使用性、語言與表達。

- 90 至 100：可直接交付。
- 80 至 89：通過，有可忍受問題。
- 60 至 79：需修正。
- 0 至 59：不通過。

## 回傳格式

每支 skill 一檔：<報告目錄>/<skill目錄名>_品質判定.json。頂層欄位：

- `version`: 2
- `skill`: skill 目錄名（與存證檔名前綴一致）
- `criteria`: 固定清單 ["任務完成度", "輸入忠實度", "規格與風險遵守",
  "可直接使用性", "語言與表達"]
- `cases`: 逐案陣列。每案含 `scores`（五維度各 0–20）、`total_score`（五維加總）、
  `critical_issues`、`tolerable_issues`、`findings`（都是文字陣列，findings 要可核對）
- `average_score`: 逐案 total_score 的平均
- `critical_issues`／`tolerable_issues`: 全案彙總
- `verdict`: 依分數與嚴重問題四級判定
- `summary`: 一句話總結
"""


# ── 輸出 ──────────────────────────────────────────────────────────────

def write_report(records: list[Record], out_dir: str) -> str:
    tally: Counter[str] = Counter(r.verdict for r in records)
    lines: list[str] = [
        "# 判定報告", "", "## 摘要",
        f"- Skill 數量：{len(records)}",
        f"- 可直接交付：{tally.get('可直接交付', 0)}",
        f"- 通過，有可忍受問題：{tally.get('通過，有可忍受問題', 0)}",
        f"- 需修正：{tally.get('需修正', 0)}",
        f"- 品質未判定：{tally.get('品質未判定', 0)}",
        f"- 待補驗：{tally.get('待補驗', 0)}",
        f"- 不通過：{tally.get('不通過', 0)}",
    ]
    if tally.get("品質未判定", 0):
        lines += ["", "## 品質未判定的原因",
                  "品質判定檔缺少，或不符合 100 分判定格式。"]
    if tally.get("待補驗", 0):
        lines += ["", "## 待補驗的原因",
                  "待補驗表示沒有足夠的存證資料可供檢查。"]
    order = ("可直接交付", "通過，有可忍受問題", "需修正", "品質未判定", "待補驗", "不通過")
    for r in sorted(records, key=lambda x: (order.index(x.verdict), x.skill)):
        lines += ["", f"## {r.skill}", f"- 狀態：{r.verdict}",
                  f"- 存證檔：{'、'.join(r.evidence) or '無'}",
                  f"- 案例數：{r.cases}"]
        if r.exercised:
            lines.append("- 已執行檢查：")
            lines += [f"  - {item}" for item in r.exercised]
        lines.append("- 內容品質：")
        if r.quality_verdict == "未判定":
            lines.append("  - 狀態：未判定")
        else:
            lines.append(f"  - 狀態：{r.quality_verdict}")
            lines.append(f"  - 平均分數：{r.quality_score:.1f}/100")
            lines.append(f"  - 判定檔：{r.quality_file}")
        if r.quality_summary:
            lines.append(f"  - 摘要：{r.quality_summary}")
        if r.quality_critical_issues:
            lines.append("  - 嚴重問題：")
            lines += [f"    - {item}" for item in r.quality_critical_issues]
        if r.quality_tolerable_issues:
            lines.append("  - 可忍受問題：")
            lines += [f"    - {item}" for item in r.quality_tolerable_issues]
        if r.quality_findings:
            lines.append("  - 逐案結果：")
            lines += [f"    - {item}" for item in r.quality_findings]
        if r.note_blind:
            lines.append(f"- 判定原因：{r.note_blind}")
        if r.note_language:
            lines.append(f"- 說明：{r.note_language}")
        if r.problems:
            lines.append("- 問題：")
            lines += [f"  - {p}" for p in r.problems]

    md = os.path.join(out_dir, "判定報告.md")
    with open(md, "w", encoding="utf-8") as f:
        _ = f.write("\n".join(lines) + "\n")
    with open(os.path.join(out_dir, "判定報告.json"), "w", encoding="utf-8") as f:
        json.dump([asdict(r) for r in records], f, ensure_ascii=False, indent=2)
    return md


def main() -> int:
    ap = argparse.ArgumentParser(add_help=False)
    _ = ap.add_argument("paths", nargs="*")
    _ = ap.add_argument("--out-dir", default="驗證報告")
    _ = ap.add_argument("--evidence-dir", default=None)
    _ = ap.add_argument("--selftest", action="store_true")
    _ = ap.add_argument("-h", "--help", action="store_true")
    args = ap.parse_args()

    if cast("bool", args.selftest):
        # 用子行程跑，不 import——judge_selftest 會 import judge，
        # 直接匯入就是循環相依。
        return subprocess.run(
            [sys.executable, os.path.join(_HERE, "judge_selftest.py")],
            check=False).returncode
    paths = cast("list[str]", args.paths)
    if cast("bool", args.help) or not paths:
        print(__doc__)
        return 0

    skill_dirs = find_skill_dirs(paths)
    if not skill_dirs:
        print("找不到任何含 tool.yaml 的目錄")
        return 2

    out_dir = cast("str", args.out_dir)
    os.makedirs(out_dir, exist_ok=True)
    # 存證位置依序找：--evidence-dir → $SKILL_EVIDENCE_DIR → 報告目錄。
    # 三個都沒有的話 find_evidence 還會找 skill 目錄底下，舊結構不會壞。
    ev_dir = (cast("str | None", args.evidence_dir)
              or os.environ.get("SKILL_EVIDENCE_DIR") or out_dir)

    records = [judge_skill(d, ev_dir) for d in skill_dirs]
    md = write_report(records, out_dir)

    tally: Counter[str] = Counter(r.verdict for r in records)
    print(f"判定 {len(records)} 支：" + "｜".join(
        f"{MARK[k]} {k} {tally.get(k, 0)}" for k in (
            "可直接交付", "通過，有可忍受問題", "需修正", "品質未判定", "待補驗", "不通過")))
    for r in records:
        print(f"  {MARK[r.verdict]} {r.skill[:44]:<44} {r.verdict}"
              + (f"｜沒驗到 {len(r.idle)} 項" if r.idle else ""))
        for p in r.problems:
            print(f"      {p}")
    print(f"\n判定中間報告：{md}")
    return 1 if tally.get("不通過") else 0


if __name__ == "__main__":
    sys.exit(main())
