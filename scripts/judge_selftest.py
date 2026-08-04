#!/usr/bin/env python3
"""judge.py 的對照組。跑 `python3 scripts/judge.py --selftest`。

判定器自己也會錯。寫完不先拿「已知有問題」和「已知沒問題」各餵一遍，
就不知道它是真的在判，還是每支都印綠燈。

兩邊都要測：
  正例  植入一個已知缺陷 → 判定器必須抓到，而且要抓到「那一項」不是別項
  反例  長得很像缺陷但其實正確 → 判定器必須放行

反例是這裡的重點。同名不同義（`total_cost_ceiling` 是上限不是加總）
造成的誤判，跟漏抓一樣糟：它會讓人去改一份本來就對的檔案。
"""

import json
import os
import shutil
import sys
import tempfile
import time
from typing import cast

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

import judge  # pyright: ignore[reportImplicitRelativeImport]

TOOL_YAML = """name: selftest_tool
kind: llm_generation
description: |
  做什麼：對照組用的假 skill，只給 judge.py 自我驗證用。
  不要用在：任何真實用途。
  常見說法：「對照組」「自我測試」。
skill_path: SKILL.md
input_schema:  schemas/request.json
output_schema: schemas/response.json
"""

NUM: dict[str, object] = {"type": "integer"}
OBJ: dict[str, object] = {"type": "object"}

RESPONSE_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["title", "grade", "score_total", "score_breakdown", "items", "item_count"],
    "properties": {
        "title": {"type": "string"},
        "grade": {"type": "string", "enum": ["A", "B", "C"]},
        "score_total": {"type": "integer", "minimum": 0, "maximum": 100},
        "score_breakdown": {
            "type": "object",
            "additionalProperties": False,
            "required": ["speed", "quality", "cost"],
            "properties": {"speed": NUM, "quality": NUM, "cost": NUM},
        },
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "amount"],
                "properties": {"name": {"type": "string"}, "amount": NUM},
            },
        },
        "item_count": NUM,
    },
}

GOOD: dict[str, object] = {
    "title": "對照組",
    "grade": "A",
    "score_total": 90,
    "score_breakdown": {"speed": 30, "quality": 40, "cost": 20},
    "items": [{"name": "甲", "amount": 100}, {"name": "乙", "amount": 200}],
    "item_count": 2,
}


def build_skill(root: str, extra_props: dict[str, object] | None = None,
                n_examples: int = 1, declare_lang: bool = True) -> str:
    """建一支假 skill。

    extra_props：測「額外欄位」情境時把它併進 schema，否則 additionalProperties
    會先擋下來，蓋掉真正要測的那一項——一次只讓一個變因動。
    declare_lang：SKILL.md 有沒有宣告「一律繁體中文輸出」。沒宣告時語系由平台決定，
    判定器不判語系。
    """
    d = os.path.join(root, "skill_00_對照組")
    os.makedirs(os.path.join(d, "schemas"))
    schema = cast("dict[str, object]", json.loads(json.dumps(RESPONSE_SCHEMA)))
    if extra_props:
        cast("dict[str, object]", schema["properties"]).update(extra_props)

    lang = "\n- 一律以繁體中文（台灣用語）輸出\n" if declare_lang else ""
    write(os.path.join(d, "tool.yaml"), TOOL_YAML)
    write(os.path.join(d, "SKILL.md"), "# 對照組\n\n只給 judge.py 自我驗證用。\n" + lang)
    write_json(os.path.join(d, "schemas", "response.json"), schema)
    write_json(os.path.join(d, "schemas", "request.json"), {"type": "object"})
    examples: list[dict[str, object]] = [
        {"user_query": f"測{i}", "input": {}} for i in range(n_examples)]
    write_json(os.path.join(d, "examples.json"), examples)
    return d


def write(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        _ = f.write(text)


def write_json(path: str, data: object) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def case(raw: object, **extra: object) -> dict[str, object]:
    body = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
    rec: dict[str, object] = {
        "test": "測試1_對照組", "model": "selftest-model", "input": {},
        "raw_response": body, "parse_ok": True, "schema_check": "必填 6/6",
        "checks": {"simplified_hits": 0}, "elapsed_sec": 0,
    }
    rec.update(extra)
    return rec


def write_evidence(skill_dir: str, cases: list[dict[str, object]]) -> str:
    p = os.path.join(skill_dir, "驗證結果.json")
    write_json(p, cases)
    # 存證要比 SKILL.md 新，否則每個案例都會被 J8 判存證過期，蓋掉要測的那一項
    now = time.time() + 5
    os.utime(p, (now, now))
    return p


def quality_file(skill: str, critical: list[str]) -> dict[str, object]:
    scores = {name: 20 for name in judge.QUALITY_DIMENSIONS}
    verdict = "不通過" if critical else "可直接交付"
    return {
        "version": 2,
        "skill": skill,
        "criteria": list(judge.QUALITY_DIMENSIONS),
        "cases": [{
            "case": 1,
            "scores": scores,
            "total_score": 100,
            "critical_issues": [],
            "tolerable_issues": [],
            "findings": ["對照組結果"],
        }],
        "average_score": 100,
        "critical_issues": critical,
        "tolerable_issues": [],
        "verdict": verdict,
        "summary": "對照組品質判定",
    }


def run() -> int:
    root = tempfile.mkdtemp(prefix="judge_selftest_")
    tally = {"ok": 0, "ng": 0}

    def check(name: str, cases: list[dict[str, object]], want_code: str | None,
              want_verdict: str, extra_props: dict[str, object] | None = None,
              n_examples: int = 1, declare_lang: bool = True) -> None:
        """want_code=None 代表這一組必須零問題（反例）。"""
        d = build_skill(tempfile.mkdtemp(prefix="s_", dir=root),
                        extra_props, n_examples, declare_lang)
        _ = write_evidence(d, cases)
        rec = judge.judge_skill(d, None)
        hit = (any(p.startswith(f"[{want_code} ") for p in rec.problems)
               if want_code else not rec.problems)
        if hit and rec.verdict == want_verdict:
            tally["ok"] += 1
            print(f"  ✓ {name}")
            return
        tally["ng"] += 1
        print(f"  ✗ {name}")
        print(f"      期望：{want_code or '零問題'}／判定 {want_verdict}")
        print(f"      實得：判定 {rec.verdict}")
        for p in rec.problems:
            print(f"        {p}")

    print("=" * 66)
    print("judge.py 對照組")
    print("=" * 66)

    print("\n[反例] 這些必須放行，抓到就是誤判")
    check("完全正確的輸出", [case(GOOD)], None, "品質未判定")

    # 上限 = 各項上限相加，不是實際花費相加。名字像總計，語意是兩回事。
    # 同時 score_total 同層多了一組不相干的數字，名字對不上就不該配對——
    # 這是判定器最容易誤判的地方，也是這個對照組存在的主要理由。
    ceiling = dict(GOOD, total_cost_ceiling=999,
                   cost_detail={"fixed": 100, "material": 200})
    check("上限欄位不當成加總，且不與不相干明細配對", [case(ceiling)], None, "品質未判定",
          extra_props={"total_cost_ceiling": NUM, "cost_detail": OBJ})

    single = dict(GOOD, fee_total=50, fee_note={"only": 7})  # 單欄位不構成明細
    check("單欄位物件不當成明細加總", [case(single)], None, "品質未判定",
          extra_props={"fee_total": NUM, "fee_note": OBJ})

    # 算式紀錄不是加項清單：5 晚 × 800 元 = 4000。把 units 跟 rate 加進金額毫無意義。
    # 這一組取自實際誤判——判定器原本報「total_fee=4000 但加總是 4805」。
    formula = dict(GOOD, total_fee=4000,
                   base_fee={"units": 5, "rate": 800, "subtotal": 4000})
    check("算式紀錄（數量×單價=小計）不當成加項清單", [case(formula)], None, "品質未判定",
          extra_props={"total_fee": NUM, "base_fee": OBJ})

    check("SKILL.md 沒宣告語言時，簡體字不算缺陷",
          [case(dict(GOOD, title="他们发过对照组"))], None, "品質未判定", declare_lang=False)

    print("\n[正例] 這些必須抓到")

    dup = json.dumps(GOOD, ensure_ascii=False)
    check("J1 同一份答案印兩次", [case(dup + dup, parse_ok=False)], "J1", "不通過")
    check("J1 尾端截斷", [case(dup[:-12], parse_ok=False)], "J1", "不通過")
    check("J1 帶 markdown 圍欄", [case("```json\n" + dup + "\n```")], "J1", "不通過")
    check("J1 空輸出", [case("", parse_ok=False)], "J1", "不通過")

    check("J2 enum 不合法", [case(dict(GOOD, grade="甲"))], "J2", "不通過")
    check("J2 數值超出 maximum", [case(dict(GOOD, score_total=120))], "J2", "不通過")
    check("J2 多了未宣告欄位", [case(dict(GOOD, note="schema 沒宣告"))], "J2", "不通過")
    check("J2 缺必填欄位",
          [case({k: v for k, v in GOOD.items() if k != "grade"})], "J2", "不通過")

    # 30+40+20 = 90，宣稱 95
    check("J3 總分對不上七維加總", [case(dict(GOOD, score_total=95))], "J3", "不通過")
    # items[].amount 實為 300，宣稱 500
    check("J3 總額對不上明細陣列加總", [case(dict(GOOD, amount_total=500))], "J3", "不通過",
          extra_props={"amount_total": NUM})

    check("J6 宣告繁中卻出現簡體字",
          [case(dict(GOOD, title="他们发过对照组"))], "J6", "不通過")

    check("J7 model 欄位沒填", [case(GOOD, model="")], "J7", "不通過")
    check("J7 存證案例數少於 examples", [case(GOOD)], "J7", "不通過", n_examples=3)
    # 空陣列跟「沒有存證檔」不一樣：有人跑了測試卻一筆都沒存下來，那是壞了。
    # 完全沒有存證檔才是「還沒驗」。
    check("J7 存證是空陣列", [], "J7", "不通過")

    check("J5 沒照抄腳本算的數字", [case(GOOD, tool_calls=[{
        "payload": "{}",
        "result": json.dumps({"score_total": 88, "grade": "A"},   # 腳本 88，輸出寫 90
                             ensure_ascii=False)}])], "J5", "不通過")
    check("J5 照抄了就放行", [case(GOOD, tool_calls=[{
        "payload": "{}",
        "result": json.dumps({"score_total": 90, "grade": "A",
                              "rule_note": "腳本自己的稽核欄位，輸出沒有也不算缺"},
                             ensure_ascii=False)}])], None, "品質未判定")
    # 腳本把同一個值放兩次、模型只寫一次，值一樣就不算缺漏。取自實際誤判。
    check("J5 腳本重複回傳同一個值不算缺漏", [case(GOOD, tool_calls=[{
        "payload": "{}",
        "result": json.dumps({"score_total": 90,
                              "comparison": {"score_total": 90}},
                             ensure_ascii=False)}])], None, "品質未判定")
    # 列舉值被改寫成人話是正常的，不是竄改。取自實際誤判。
    check("J5 字串被改寫成人話不算竄改", [case(dict(GOOD, title="未指定，採市場慣例"),
        tool_calls=[{"payload": "{}",
                     "result": json.dumps({"title": "market_default", "score_total": 90},
                                          ensure_ascii=False)}])], None, "品質未判定")

    print("\n[J8] 存證比 SKILL.md 舊")
    d = build_skill(tempfile.mkdtemp(prefix="s_", dir=root))
    p = write_evidence(d, [case(GOOD)])
    old = time.time() - 3600
    os.utime(p, (old, old))
    rec = judge.judge_skill(d, None)
    if any(x.startswith("[J8 ") for x in rec.problems) and rec.verdict == "不通過":
        tally["ok"] += 1
        print("  ✓ J8 存證過期")
    else:
        tally["ng"] += 1
        print(f"  ✗ J8 存證過期｜實得 {rec.verdict}：{rec.problems}")

    print("\n[品質規則] 嚴重問題優先於 100 分")
    quality_cases = [
        (95, [], "可直接交付"),
        (85, [], "通過，有可忍受問題"),
        (70, [], "需修正"),
        (95, ["退款金額錯誤"], "不通過"),
    ]
    for score, critical, expected in quality_cases:
        got = judge.quality_outcome(score, critical)
        if got == expected:
            tally["ok"] += 1
            print(f"  ✓ {score} 分／嚴重問題 {len(critical)} 項：{expected}")
        else:
            tally["ng"] += 1
            print(f"  ✗ {score} 分／嚴重問題 {len(critical)} 項：期望 {expected}，實得 {got}")

    print("\n[品質檔] 100 分資料必須影響最終判定")
    d = build_skill(tempfile.mkdtemp(prefix="s_", dir=root))
    _ = write_evidence(d, [case(GOOD)])
    name = os.path.basename(d)
    write_json(os.path.join(root, f"{name}_品質判定.json"), quality_file(name, []))
    rec = judge.judge_skill(d, root)
    if rec.verdict == "可直接交付" and rec.quality_score == 100:
        tally["ok"] += 1
        print("  ✓ 合法品質檔判為可直接交付")
    else:
        tally["ng"] += 1
        print(f"  ✗ 合法品質檔｜實得 {rec.verdict}：{rec.problems}")

    write_json(os.path.join(root, f"{name}_品質判定.json"), quality_file(name, ["退款金額錯誤"]))
    rec = judge.judge_skill(d, root)
    if rec.verdict == "不通過" and rec.quality_critical_issues:
        tally["ok"] += 1
        print("  ✓ 100 分但有嚴重問題仍判不通過")
    else:
        tally["ng"] += 1
        print(f"  ✗ 嚴重問題品質檔｜實得 {rec.verdict}：{rec.problems}")

    print("\n[盲區] 沒有存證時要說「沒驗到」，不能印綠燈")
    rec = judge.judge_skill(build_skill(tempfile.mkdtemp(prefix="s_", dir=root)), None)
    if rec.verdict == "待補驗" and len(rec.idle) == len(judge.DIMENSIONS):
        tally["ok"] += 1
        print("  ✓ 無存證判「待補驗」，八項全列為沒驗到")
    else:
        tally["ng"] += 1
        print(f"  ✗ 無存證｜實得 {rec.verdict}｜沒驗到 {len(rec.idle)} 項")

    shutil.rmtree(root, ignore_errors=True)
    print("\n" + "=" * 66)
    print(f"對照組：通過 {tally['ok']}、不通過 {tally['ng']}")
    print("=" * 66)
    return 1 if tally["ng"] else 0


if __name__ == "__main__":
    sys.exit(run())
