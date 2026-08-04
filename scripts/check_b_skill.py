#!/usr/bin/env python3
"""Skill 機械檢查（零 LLM、零額度）

用法：
  python3 check_b_skill.py <skill目錄或上層目錄> [更多目錄...] [--out-dir 報告輸出目錄]

上層目錄會自動掃描底下含 tool.yaml 的子目錄。
輸出：
  <out-dir>/機械檢查_報告.md   （人讀）
  <out-dir>/機械檢查_報告.json （LLM 檢查階段直接吃）

檢查項（ERROR = 必修；WARN = 建議修或需人工判斷）：
  1. 檔案齊全（tool.yaml / SKILL.md / schemas/request.json / schemas/response.json / examples.json）
  2. tool.yaml 6 必填 + name 格式 + kind + 路徑對應 + vendor
     contact：必填、須合法 email、不得是範例預設值（ERROR 級，因為它是平台的對外窗口）
  3. examples.json：至少 3 組，每組 key 用 user_query + input
     input 的內容不驗 schema，範例品質由品質判定把關
  4. schemas JSON 合法
  5. SKILL.md 三條必寫規則（不反問 / 待補禁編造 / 繁體中文）
  6. SKILL.md 覆蓋 response.json 頂層必填欄位名
  7. SKILL.md 覆蓋 response schema 內的中文 enum 值
  8. 可計數格式條款有沒有但書
  9. 簡體字掃描
 10. 外部呼叫指示（排除否定句）
 11. 去識別化（examples / SKILL.md / schemas 不得出現自行設定的真實資訊）
 12. name 撞名（本批次內 + 對照已完成Skills）
"""

import sys, os, re, json, argparse, unicodedata
from datetime import datetime

# ── 常數 ──────────────────────────────────────────────────────────

REQUIRED_FILES = ["tool.yaml", "SKILL.md", "schemas/request.json",
                  "schemas/response.json", "examples.json"]
TOOL_REQUIRED = ["name", "kind", "description", "skill_path",
                 "input_schema", "output_schema"]

# 與 verify_completed_skills.py 同源（已排除繁簡同形的「只」等字）
SIMPLIFIED_CHARS = set(
    "们后发过对说时国办书门问题业务应该让员报记这门东车间开关从产厂广场头"
    "买卖钱银级红绿颜风飞马鸟鱼龙园远运达迟适选边连进违结给绝统继续绩网络"
    "询证语议论识设访许诉调贵费资赛购贯现观规视览觉观计订认讲证评识说读课"
    "谁调请谈执图书战术"
)
# 繁簡同形白名單（掃描時跳過，避免誤報）
SIMPLIFIED_SAFE = set("问题业务应该员报记这东车间开关从产头买卖级风远运达适选边连进结给统绩网证语设访许调费资购现观规视计订认讲评说读课请谈执图书战术时国办门场廣")

# 可攜工具包不內建任何公司或個人資料。需要去識別化檢查時，在執行環境設定
# SKILL_DEID_BLOCKLIST，以半形逗號分隔要禁止的字串。
DEID_BLOCKLIST = [item.strip() for item in
                  os.environ.get("SKILL_DEID_BLOCKLIST", "").split(",")
                  if item.strip()]

# 範例檔沿用未改的預設值（平台 example_A / example_B 用的假 email）
PLACEHOLDER_CONTACTS = {"jane@acme-seo.com", "jane@acme.com",
                        "you@example.com", "contact@example.com"}
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

EXTERNAL_CALL_RE = re.compile(r"https?://|curl\s|wget\s|呼叫\s*API|外部\s*API|執行程式|執行腳本")
NEGATION_RE = re.compile(r"不|禁止|不得|勿|沒有|排除|禁用|避免")

RULE_NO_FOLLOWUP = re.compile(r"(不|禁止|不得|勿).{0,10}(反問|追問|詢問)|一次(性|完整)?.{0,6}產出")
RULE_NO_FABRICATE = re.compile(r"(不|禁止|不得|勿).{0,8}(編造|捏造|虛構)|待補|待確認|待指定")
RULE_LANGUAGE = re.compile(r"繁體中文")

COUNT_CLAUSE_RE = re.compile(
    r"(至少|最多|剛好)\s*[0-9一二三四五六七八九十]+\s*(點|項|條|個|種|欄|組)"
    r"|[0-9]+\s*[-–~至到]\s*[0-9]+\s*(點|項|條|個|種|組)")
PROVISO_RE = re.compile(r"待補|即符合|未提供|沒提供|沒有提供|無法.{0,4}時|null")

CJK_RE = re.compile(r"[一-鿿]")

# ── 簡易 YAML 解析（頂層 key + | 多行區塊就夠了）───────────────────

def parse_tool_yaml(text):
    data, cur_key, block, block_mode = {}, None, [], False
    for line in text.split("\n"):
        if block_mode:
            if line.startswith(("  ", "\t")) or line.strip() == "":
                block.append(line.strip())
                continue
            data[cur_key] = "\n".join(block).strip()
            block_mode = False
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$", line)
        if m and not line.startswith((" ", "\t")):
            key, val = m.group(1), m.group(2)
            val = re.sub(r"\s+#.*$", "", val).strip()   # 去行尾註解
            if val in ("|", "|-", ">", ">-"):
                cur_key, block, block_mode = key, [], True
            else:
                data[key] = val.strip('"').strip("'")
    if block_mode:
        data[cur_key] = "\n".join(block).strip()
    return data

# ── 掃描輔助 ──────────────────────────────────────────────────────

def collect_enum_cjk(schema, acc):
    if isinstance(schema, dict):
        for v in schema.get("enum", []):
            if isinstance(v, str) and CJK_RE.search(v):
                acc.add(v)
        for v in schema.values():
            collect_enum_cjk(v, acc)
    elif isinstance(schema, list):
        for v in schema:
            collect_enum_cjk(v, acc)

def scan_simplified(text):
    hits = []
    for i, line in enumerate(text.split("\n"), 1):
        for ch in line:
            if ch in SIMPLIFIED_CHARS and ch not in SIMPLIFIED_SAFE:
                hits.append((i, ch, line.strip()[:60]))
    return hits

# ── 單一 skill 檢查 ───────────────────────────────────────────────

def check_skill(skill_dir):
    findings = []   # (level, code, message)
    name = os.path.basename(skill_dir.rstrip("/"))
    E = lambda c, m: findings.append(("ERROR", c, m))
    W = lambda c, m: findings.append(("WARN", c, m))

    # 1. 檔案齊全
    missing = [f for f in REQUIRED_FILES
               if not os.path.isfile(os.path.join(skill_dir, f))]
    for f in missing:
        E("missing_file", f"缺少 {f}")

    texts = {}
    for f in REQUIRED_FILES:
        p = os.path.join(skill_dir, f)
        if os.path.isfile(p):
            with open(p, encoding="utf-8") as fh:
                texts[f] = fh.read()

    # 2. tool.yaml
    tool = {}
    phrases = []
    if "tool.yaml" in texts:
        tool = parse_tool_yaml(texts["tool.yaml"])
        for k in TOOL_REQUIRED:
            if not tool.get(k):
                E("tool_yaml", f"tool.yaml 缺必填 {k}")
        if tool.get("name") and not re.match(r"^[a-z][a-z0-9_]*$", tool["name"]):
            E("tool_yaml", f"name 格式錯誤（要英文小寫+底線）：{tool['name']}")
        if tool.get("kind") and tool["kind"] not in ("llm_generation", "mcp_server"):
            E("tool_yaml", f"kind 應為 llm_generation 或 mcp_server，實際：{tool['kind']}")
        desc = tool.get("description", "")
        if desc and "做什麼" not in desc:
            W("tool_yaml", "description 建議用「做什麼：」開頭（照平台範例）")
        if desc and ("不要用在" not in desc and "不適用" not in desc):
            W("tool_yaml", "description 沒寫「不要用在」，建議補（避免 AI 派錯單）")
        # 常見說法（口語觸發詞）＝使用者實際會打字的問句關鍵詞，
        # 派單模型靠它決定派不派給這支。沒寫→建議補（WARN）；有寫→驗證內容。
        if desc:
            if "常見說法" not in desc:
                W("tool_yaml", "description 沒寫「常見說法」——建議補：使用者會打的口語觸發詞（派單靠它）")
            else:
                phrases = re.findall(r"「([^」]+)」", desc.split("常見說法", 1)[1])
                if not phrases:
                    W("tool_yaml", "「常見說法」有寫但觸發語沒用「」包住，機測驗不到內容")
                else:
                    dup_in_self = sorted({p for p in phrases if phrases.count(p) > 1})
                    if dup_in_self:
                        W("tool_yaml", f"常見說法同一句重複宣告：{dup_in_self}")
        for k, expect in [("skill_path", "SKILL.md"),
                          ("input_schema", "schemas/request.json"),
                          ("output_schema", "schemas/response.json")]:
            if tool.get(k) and tool[k] != expect:
                E("tool_yaml", f"{k} 應為 {expect}，實際：{tool[k]}")
        # contact 必填且必須是作者本人：平台文件定義為「串接有問題時我們找誰」，
        # 空值等於交件後沒有窗口，所以是 ERROR 不是 WARN。
        contact = tool.get("contact", "")
        if not contact:
            E("tool_yaml", "contact 未填——必填，填實際作者的 email（平台會直接找這個人）")
        elif not EMAIL_RE.match(contact):
            E("tool_yaml", f"contact 不是合法 email：{contact!r}")
        elif contact in PLACEHOLDER_CONTACTS:
            E("tool_yaml", f"contact 還是範例檔的預設值沒改：{contact}")
        if not tool.get("vendor"):
            W("tool_yaml", "vendor 未填（填交件單位名稱）")

    # 3. schemas
    schemas = {}
    for f in ("schemas/request.json", "schemas/response.json"):
        if f in texts:
            try:
                schemas[f] = json.loads(texts[f])
            except json.JSONDecodeError as e:
                E("schema_json", f"{f} 不是合法 JSON：{e}")

    # 4. examples.json
    examples = None
    if "examples.json" in texts:
        try:
            examples = json.loads(texts["examples.json"])
        except json.JSONDecodeError as e:
            E("examples_json", f"examples.json 不是合法 JSON：{e}")
    if isinstance(examples, list):
        # 平台《AI 工具提交說明文件》§2.3 寫的是「至少 3 組」，不是剛好 3 組。
        # 原本卡死 != 3，會把合規的交件報成 WARN，變成拿內規當退件理由。以客戶規格為準。
        if len(examples) < 3:
            W("examples", f"範例只有 {len(examples)} 組（平台 §2.3 要求至少 3 組）")
        # 機測只管外層 key 名。input 的內容（缺必填、多欄位、enum 對不上）
        # 由品質判定把關，這裡放行。
        for i, ex in enumerate(examples, 1):
            keys = set(ex.keys()) if isinstance(ex, dict) else set()
            wrong = keys & {"api_input", "skill_input", "request", "params"}
            if "input" not in keys:
                if wrong:
                    E("examples_key", f"第 {i} 組用了 {sorted(wrong)}，規格要求 key 名是 input")
                else:
                    E("examples_key", f"第 {i} 組缺 input")
            if "user_query" not in keys:
                W("examples_key", f"第 {i} 組缺 user_query（平台範例格式有）")
            extra = keys - {"user_query", "input"} - wrong
            if extra:
                W("examples_key", f"第 {i} 組多出 key：{sorted(extra)}（平台範例只有 user_query + input）")
    elif examples is not None:
        E("examples", "examples.json 頂層要是陣列")

    # 5~8. SKILL.md
    skill_md = texts.get("SKILL.md", "")
    if skill_md:
        if not RULE_NO_FOLLOWUP.search(skill_md):
            E("b_rules", "SKILL.md 沒寫「一次產出不反問」規則")
        if not RULE_NO_FABRICATE.search(skill_md):
            E("b_rules", "SKILL.md 沒寫「缺料標待補、禁止編造」規則")
        if not RULE_LANGUAGE.search(skill_md):
            E("b_rules", "SKILL.md 沒明文指定繁體中文輸出")

        resp = schemas.get("schemas/response.json")
        if isinstance(resp, dict):
            uncovered = [k for k in resp.get("required", [])
                         if k not in skill_md]
            if uncovered:
                W("field_coverage",
                  f"SKILL.md 沒提到 response 必填欄位：{uncovered}（靠 examples 撐，建議步驟裡教）")
            enum_vals = set()
            collect_enum_cjk(resp, enum_vals)
            un_enum = sorted(v for v in enum_vals if v not in skill_md)
            if un_enum:
                W("enum_coverage",
                  f"schema 中文 enum 值沒在 SKILL.md 出現：{un_enum}（角色/類別定義不一致的訊號）")

        clauses = sorted({m.group(0) for m in COUNT_CLAUSE_RE.finditer(skill_md)})
        if clauses and not PROVISO_RE.search(skill_md):
            W("count_clause",
              f"有可計數條款 {clauses} 但全文沒有但書字眼（待補/未提供時即符合）")

        for i, line in enumerate(skill_md.split("\n"), 1):
            if EXTERNAL_CALL_RE.search(line) and not NEGATION_RE.search(line):
                W("external_call", f"SKILL.md L{i} 疑似外部呼叫指示：{line.strip()[:70]}")

    # 9. 簡體字（全部檔案）
    for f, txt in texts.items():
        hits = scan_simplified(txt)
        for ln, ch, ctx in hits[:5]:
            E("simplified", f"{f} L{ln} 疑似簡體「{ch}」：{ctx}")
        if len(hits) > 5:
            E("simplified", f"{f} 另有 {len(hits)-5} 處簡體字（詳見原檔）")

    # 11. 去識別化（tool.yaml 的 contact/vendor 允許真實資訊，其他檔不行）
    for f, txt in texts.items():
        if f == "tool.yaml":
            continue
        for kw in DEID_BLOCKLIST:
            if kw in txt:
                E("deid", f"{f} 出現真實資訊「{kw}」，examples/SKILL.md 要用虛構公司")

    return {"skill_dir": skill_dir, "name": tool.get("name", name),
            "dir_name": name, "phrases": phrases, "findings": findings,
            "errors": sum(1 for x in findings if x[0] == "ERROR"),
            "warns": sum(1 for x in findings if x[0] == "WARN")}

# ── 主流程 ────────────────────────────────────────────────────────

def find_skill_dirs(paths):
    dirs = []
    for p in paths:
        p = p.rstrip("/")
        if os.path.isfile(os.path.join(p, "tool.yaml")):
            dirs.append(p)
        elif os.path.isdir(p):
            for root, subdirs, files in os.walk(p):
                subdirs[:] = [d for d in subdirs
                              if not d.startswith((".", "_")) and d != "__MACOSX"]
                if "tool.yaml" in files:
                    dirs.append(root)
                    subdirs[:] = []
    return sorted(set(dirs))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--out-dir", default=None,
                    help="報告輸出目錄（預設 = 第一個輸入路徑）")
    ap.add_argument("--known-dir", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "已完成Skills"),
        help="撞名對照的已完成Skills目錄")
    args = ap.parse_args()

    skill_dirs = find_skill_dirs(args.paths)
    if not skill_dirs:
        print("找不到任何含 tool.yaml 的目錄"); sys.exit(2)

    results = [check_skill(d) for d in skill_dirs]

    # 12. 撞名檢查
    known = {}
    kd = os.path.abspath(args.known_dir)
    if os.path.isdir(kd):
        for d in find_skill_dirs([kd]):
            with open(os.path.join(d, "tool.yaml"), encoding="utf-8") as fh:
                n = parse_tool_yaml(fh.read()).get("name")
            if n:
                known[n] = d
    seen = {}
    for r in results:
        n = r["name"]
        # 被檢查的 skill 本身可能就住在 known-dir 底下（輸出目錄＝已完成Skills），
        # 這時 known[n] 指向的就是它自己，不算撞名。
        if n in known and os.path.abspath(known[n]) != os.path.abspath(r["skill_dir"]):
            r["findings"].append(("WARN", "name_dup",
                f"name「{n}」與已完成Skills 撞名：{known[n]}"))
            r["warns"] += 1
        if n in seen:
            r["findings"].append(("ERROR", "name_dup",
                f"name「{n}」與本批次 {seen[n]} 重複"))
            r["errors"] += 1
        seen.setdefault(n, r["dir_name"])

    # 常見說法觸發語撞句（本批次內）：兩支宣告同一句，派單只能二選一。
    # 全案跨區的完整衝突掃描交給 check_routing.py，這裡只顧本批次。
    phrase_owner = {}
    for r in results:
        for p in r.get("phrases", []):
            if p in phrase_owner and phrase_owner[p] != r["dir_name"]:
                r["findings"].append(("WARN", "phrase_dup",
                    f"觸發語「{p}」與本批次 {phrase_owner[p]} 重複"))
                r["warns"] += 1
            phrase_owner.setdefault(p, r["dir_name"])

    # 報告
    out_dir = args.out_dir or (args.paths[0] if os.path.isdir(args.paths[0])
                               else os.path.dirname(args.paths[0]))
    os.makedirs(out_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    total_e = sum(r["errors"] for r in results)
    total_w = sum(r["warns"] for r in results)
    md = ["# Skill 機械檢查報告", "", "## 摘要", f"- 檢查時間：{ts}",
          f"- Skill 數量：{len(results)}", f"- 錯誤：{total_e}",
          f"- 警告：{total_w}", ""]
    md.append("| Skill | 目錄 | 錯誤 | 警告 |")
    md.append("|---|---|---:|---:|")
    for r in results:
        md.append(f"| {r['name']} | {r['dir_name']} | {r['errors']} | {r['warns']} |")
    md.append("")
    for r in results:
        md += [f"## {r['name']}", f"- 目錄：{r['dir_name']}"]
        if not r["findings"]:
            md.append("- 結果：通過。")
        for lv, code, msg in r["findings"]:
            level = "錯誤" if lv == "ERROR" else "警告"
            md.append(f"- {level} [{code}]：{msg}")
        md.append("")

    md_path = os.path.join(out_dir, "機械檢查_報告.md")
    json_path = os.path.join(out_dir, "機械檢查_報告.json")
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(md))
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump({"generated_at": ts, "results": [
            {**r, "findings": [list(x) for x in r["findings"]]}
            for r in results]}, fh, ensure_ascii=False, indent=1)

    print(f"檢查 {len(results)} 個 skill：ERROR {total_e}、WARN {total_w}")
    for r in results:
        flag = "✗" if r["errors"] else ("△" if r["warns"] else "✓")
        print(f"  {flag} {r['name']:40s} E{r['errors']} W{r['warns']}")
    print(f"報告：{md_path}")
    print(f"JSON：{json_path}")
    sys.exit(1 if total_e else 0)

if __name__ == "__main__":
    main()
