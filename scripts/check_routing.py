#!/usr/bin/env python3
"""派單路由衝突檢查：找出兩支以上 skill 會搶同一句使用者問題的地方。

為什麼要這一關：平台《AI 工具提交說明文件》§4 第 4 點寫明，交件後平台會
「用 20 條模擬客戶問題測試 AI 會不會正確把任務派給您（目標 ≥ 90%）」。
派單靠的就是 tool.yaml 的 description。一批 skill 裡光是「應記載事項檢查」就可能有
旅遊契約、生前契約、不動產說明書、TTQS 四支，descriptions 只要互相打架，
這一關就會被擋下來。

這支腳本不判斷「派得對不對」——那要實際跑模型。它只做機械檢查：
哪些觸發語被多支 skill 同時宣告，以及宣告的雙方有沒有在「不要用在」互相排除。

用法：
    python3 scripts/check_routing.py                 總表
    python3 scripts/check_routing.py --pairs         加印每一組衝突的雙方原文
    python3 scripts/check_routing.py --csv           輸出 CSV
"""
import csv
import re
import sys
from collections import defaultdict
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent

# Skill 目錄位於分區目錄下，兩個根目錄都要掃。派單路由是全案的事——
# 待驗的 skill 一樣會跟已完成的搶同一句話，漏掉它們等於漏掉一半的衝突。
# 可攜補丁（驗證工具包版）：位置參數當掃描根目錄，沒給才用本專案預設兩區。
_arg_roots = tuple(Path(a) for a in sys.argv[1:] if not a.startswith("--"))
ROOTS = _arg_roots or (BASE / "已完成Skills", BASE / "待驗Skills")

# description 的三段式格式。平台沒有規定要這樣寫，是我們自己的寫法，
# 缺段代表這支的觸發條件沒交代清楚，派單模型只能用「做什麼」去猜。
SECTIONS = ("做什麼", "不要用在", "常見說法")

# 觸發語寫在全形引號裡。用非貪婪比對，一句一個。
PHRASE_RE = re.compile(r"「([^」]+)」")


class Skill:
    """一支 skill 的 description 拆解結果。"""

    def __init__(self, name: str, title: str, desc: str) -> None:
        self.name: str = name
        self.title: str = title
        self.desc: str = desc
        self.sections: dict[str, str] = split_sections(desc)
        self.phrases: list[str] = PHRASE_RE.findall(self.sections.get("常見說法", ""))
        self.avoid: str = self.sections.get("不要用在", "")

    @property
    def missing_sections(self) -> list[str]:
        return [s for s in SECTIONS if not self.sections.get(s)]


def read_description(tool_yaml: Path) -> str:
    """抽出 tool.yaml 的 description 區塊字串。

    不用 YAML 函式庫解析，因為 description 是 `|` 區塊字串，內容裡有全形冒號
    與引號，交給通用解析器反而容易在邊界出錯。這裡只認一種格式：
    `description: |` 之後所有縮排的行，遇到第一個非縮排的非空行就停。
    """
    lines = tool_yaml.read_text(encoding="utf-8").splitlines()
    body: list[str] = []
    collecting = False
    for line in lines:
        if not collecting:
            if re.match(r"^description\s*:\s*\|", line):
                collecting = True
            continue
        if line.strip() and not line.startswith((" ", "\t")):
            break
        body.append(line.strip())
    return "\n".join(body).strip()


def split_sections(desc: str) -> dict[str, str]:
    """把 description 依「做什麼：」「不要用在：」「常見說法：」切成三段。"""
    out: dict[str, str] = {}
    positions: list[tuple[int, str]] = []
    for sec in SECTIONS:
        m = re.search(rf"{sec}\s*[：:]", desc)
        if m:
            positions.append((m.end(), sec))
    positions.sort()
    for i, (start, sec) in enumerate(positions):
        end = positions[i + 1][0] - len(positions[i + 1][1]) - 1 if i + 1 < len(positions) else len(desc)
        out[sec] = desc[start:end].strip()
    return out


def load_skills() -> list[Skill]:
    out: list[Skill] = []
    dirs = sorted(
        p for root in ROOTS if root.is_dir()
        for p in root.rglob("skill_*") if p.is_dir()
    )
    for d in dirs:
        ty = d / "tool.yaml"
        if not ty.is_file():
            continue
        title = d.name.split("_", 2)[2] if d.name.count("_") >= 2 else d.name
        out.append(Skill(d.name, title, read_description(ty)))
    return out


def excludes(a: Skill, b: Skill) -> bool:
    """a 的「不要用在」有沒有把 b 排除掉。

    機械判準：b 的標題裡任一個連續 3 字以上的片段，出現在 a 的「不要用在」裡。
    這只是下限——排除詞寫得再委婉，只要沒出現對方的關鍵字，派單模型就分不出來。
    """
    if not a.avoid:
        return False
    return any(b.title[i:i + 3] in a.avoid for i in range(len(b.title) - 2))


def main() -> None:
    show_pairs = "--pairs" in sys.argv
    as_csv = "--csv" in sys.argv
    skills = load_skills()
    by_name = {s.name: s for s in skills}

    # 一句觸發語被幾支宣告
    owners: dict[str, list[str]] = defaultdict(list)
    for s in skills:
        for p in s.phrases:
            owners[p].append(s.name)
    clashes = {p: ns for p, ns in owners.items() if len(ns) > 1}

    # 觸發語互為子字串：短的那句更泛用，會把長的那句一起吃掉
    subsets: list[tuple[str, str, str, str]] = []
    all_phrases = sorted(owners)
    for short in all_phrases:
        for long in all_phrases:
            if short != long and short in long:
                for a in owners[short]:
                    for b in owners[long]:
                        if a != b:
                            subsets.append((short, a, long, b))

    if as_csv:
        w = csv.writer(sys.stdout)
        w.writerow(["觸發語", "宣告支數", "宣告的 skill"])
        for p, ns in sorted(clashes.items(), key=lambda kv: -len(kv[1])):
            w.writerow([p, len(ns), "；".join(ns)])
        return

    print(f"skill 數 {len(skills)}｜觸發語總數 {sum(len(s.phrases) for s in skills)}"
          + f"｜相異觸發語 {len(owners)}")
    print()

    bad_struct = [s for s in skills if s.missing_sections]
    print("description 三段式結構")
    if bad_struct:
        print(f"  ❌ 缺段 {len(bad_struct)} 支")
        for s in bad_struct:
            print(f"     {s.name:<44} 缺：{'、'.join(s.missing_sections)}")
    else:
        print(f"  ✅ {len(skills)}/{len(skills)} 三段齊全")
    no_phrase = [s for s in skills if not s.phrases]
    if no_phrase:
        print(f"  ❌ 沒有任何觸發語 {len(no_phrase)} 支")
        for s in no_phrase:
            print(f"     {s.name}")
    print()

    print("觸發語完全相同（同一句話被多支宣告）")
    if not clashes:
        print("  ✅ 無")
    else:
        # 雙方都沒互相排除的排前面——那是真的會派錯的組合
        def risk(item: tuple[str, list[str]]) -> tuple[int, int]:
            _, ns = item
            unguarded = sum(
                1 for a in ns for b in ns
                if a != b and not excludes(by_name[a], by_name[b])
            )
            return (-unguarded, -len(ns))

        print(f"  ❌ {len(clashes)} 句被重複宣告")
        for p, ns in sorted(clashes.items(), key=risk):
            marks: list[str] = []
            for a in ns:
                guarded = all(excludes(by_name[a], by_name[b]) for b in ns if b != a)
                marks.append(f"{a.split('_')[1]}{'✓' if guarded else '✗'}")
            print(f"     「{p}」× {len(ns)}   {' '.join(marks)}")
        print("     ✓＝該支的「不要用在」有點名其他競爭者；✗＝沒點名，派單只能靠猜")
    print()

    print("觸發語互為子字串（短句吃掉長句）")
    if not subsets:
        print("  ✅ 無")
    else:
        seen: set[tuple[str, str]] = set()
        print(f"  ❌ {len(subsets)} 組")
        for short, a, long, b in subsets:
            key = (short, long)
            if key in seen:
                continue
            seen.add(key)
            print(f"     「{short}」({a.split('_')[1]}) ⊂ 「{long}」({b.split('_')[1]})")
    print()

    if show_pairs and clashes:
        print("=== 衝突組雙方原文 ===")
        for p, ns in sorted(clashes.items(), key=lambda kv: -len(kv[1])):
            print(f"\n「{p}」")
            for n in ns:
                s = by_name[n]
                print(f"  {s.name}")
                print(f"    做什麼　：{s.sections.get('做什麼', '(缺)')}")
                print(f"    不要用在：{s.avoid or '(缺)'}")


if __name__ == "__main__":
    main()
