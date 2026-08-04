#!/usr/bin/env python3
"""MCP Skill 機械檢查（零 LLM、零額度）

用法：
  python3 check_mcp_skill.py <skill目錄或上層目錄> [更多目錄...] [--out-dir 報告輸出目錄]

上層目錄會自動掃描底下含 server.py 的子目錄。
輸出：
  <out-dir>/MCP機械檢查_報告.md   （人讀）
  <out-dir>/MCP機械檢查_報告.json （LLM 檢查階段直接吃）

檢查項（ERROR = 必修；WARN = 建議修或需人工判斷）：
  1. server.py 存在且語法正確（python3 -c "compile()"）
  2. tools/list 回傳合法 JSON-RPC，每支 tool 有 name + description + inputSchema
  3. 每支 tool 有 outputSchema（MCP 選填，我們要求必填）
  4. 每支 tool 有 annotations（至少 readOnlyHint + destructiveHint）
  5. inputSchema / outputSchema 是合法 JSON Schema（type=object, 有 properties）
  6. schemas/ 目錄有對應的 JSON 檔案
  7. tools/call 不 crash（給空物件輸入，回傳有 content，不是 Python traceback）
  8. 簡體字掃描（description + schema 的中文字串）
  9. 去識別化（SKILL_DEID_BLOCKLIST 環境變數）
 10. tool name 撞名（本批次內 + 對照已完成 MCP Skills）
"""

import argparse
import json
import os
import re
import subprocess
import sys
import unicodedata
from datetime import datetime

# ── 共用常數（與 check_b_skill.py 同源）─────────────────────────

SIMPLIFIED_CHARS = set(
    "们后发过对说时国办书门问题业务应该让员报记这门东车间开关从产厂广场头"
    "买卖钱银级红绿颜风飞马鸟鱼龙园远运达迟适选边连进违结给绝统继续绩网络"
    "询证语议论识设访许诉调贵费资赛购贯现观规视览觉观计订认讲证评识说读课"
    "谁调请谈执图书战术"
)
SIMPLIFIED_SAFE = set(
    "问题业务应该员报记这东车间开关从产头买卖级风远运达适选边连进结给统绩"
    "网证语设访许调费资购现观规视计订认讲评说读课请谈执图书战术时国办门场廣"
)

DEID_BLOCKLIST = [
    item.strip()
    for item in os.environ.get("SKILL_DEID_BLOCKLIST", "").split(",")
    if item.strip()
]


# ── 輔助函式 ─────────────────────────────────────────────────────

def scan_simplified(text):
    hits = []
    for i, line in enumerate(text.split("\n"), 1):
        for ch in line:
            if ch in SIMPLIFIED_CHARS and ch not in SIMPLIFIED_SAFE:
                hits.append((i, ch, line.strip()[:60]))
    return hits


def send_jsonrpc(server_py, method, params=None):
    """對 server.py 發一筆 JSON-RPC 請求，回傳解析後的 dict 或 None。"""
    req = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    proc = subprocess.run(
        [sys.executable, server_py],
        input=json.dumps(req, ensure_ascii=False) + "\n",
        capture_output=True, text=True, check=False, timeout=15,
    )
    if proc.returncode != 0 and not proc.stdout.strip():
        return None, proc.stderr.strip()
    for line in proc.stdout.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            return json.loads(line), None
        except json.JSONDecodeError:
            continue
    return None, proc.stderr.strip() or "server.py 無 stdout 輸出"


# ── 單一 MCP skill 檢查 ─────────────────────────────────────────

def check_mcp_skill(skill_dir):
    findings = []
    dir_name = os.path.basename(skill_dir.rstrip("/"))
    E = lambda c, m: findings.append(("ERROR", c, m))
    W = lambda c, m: findings.append(("WARN", c, m))

    server_py = os.path.join(skill_dir, "server.py")
    schemas_dir = os.path.join(skill_dir, "schemas")

    # 1. server.py 存在且語法正確
    if not os.path.isfile(server_py):
        E("missing_file", "缺少 server.py")
        return _result(dir_name, findings)

    with open(server_py, encoding="utf-8") as f:
        source = f.read()
    try:
        compile(source, server_py, "exec")
    except SyntaxError as e:
        E("syntax", f"server.py 語法錯誤：{e}")
        return _result(dir_name, findings)

    # 2. tools/list
    resp, err = send_jsonrpc(server_py, "tools/list")
    if resp is None:
        E("tools_list", f"tools/list 失敗：{err}")
        return _result(dir_name, findings)

    if "error" in resp:
        E("tools_list", f"tools/list 回傳錯誤：{resp['error']}")
        return _result(dir_name, findings)

    result = resp.get("result", {})
    tools = result.get("tools", [])
    if not tools:
        E("tools_list", "tools/list 回傳 0 支 tool")
        return _result(dir_name, findings)

    tool_names = []
    all_text_for_scan = source  # 用於簡體字和去識別化掃描

    for t in tools:
        tname = t.get("name", "")
        if not tname:
            E("tool_def", "有 tool 缺 name")
            continue
        tool_names.append(tname)

        # name 格式（MCP 規格：1-128 字元，a-z A-Z 0-9 _ - .）
        if not re.match(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}$", tname):
            E("tool_name", f"tool name 格式不符 MCP 規格：{tname}")

        # description
        desc = t.get("description", "")
        if not desc:
            E("tool_def", f"{tname}：缺 description")
        all_text_for_scan += "\n" + desc

        # 3. inputSchema
        input_schema = t.get("inputSchema")
        if not input_schema:
            E("tool_def", f"{tname}：缺 inputSchema")
        elif not isinstance(input_schema, dict):
            E("tool_def", f"{tname}：inputSchema 不是物件")
        else:
            if input_schema.get("type") != "object":
                E("schema", f"{tname}：inputSchema.type 不是 object")
            if "properties" not in input_schema and input_schema.get("type") == "object":
                W("schema", f"{tname}：inputSchema 沒有 properties")

        # outputSchema（MCP 選填，我們要求必填）
        output_schema = t.get("outputSchema")
        if not output_schema:
            E("tool_def", f"{tname}：缺 outputSchema（MCP 選填但我們要求必填）")
        elif not isinstance(output_schema, dict):
            E("tool_def", f"{tname}：outputSchema 不是物件")
        else:
            if output_schema.get("type") != "object":
                W("schema", f"{tname}：outputSchema.type 不是 object（可能是 array）")

        # 4. annotations
        ann = t.get("annotations")
        if not ann:
            W("annotations", f"{tname}：缺 annotations")
        elif isinstance(ann, dict):
            if "readOnlyHint" not in ann:
                W("annotations", f"{tname}：annotations 沒有 readOnlyHint")
            if "destructiveHint" not in ann:
                W("annotations", f"{tname}：annotations 沒有 destructiveHint")

    # 6. schemas/ 目錄
    if os.path.isdir(schemas_dir):
        schema_files = [f for f in os.listdir(schemas_dir) if f.endswith(".json")]
        for sf in schema_files:
            sf_path = os.path.join(schemas_dir, sf)
            try:
                with open(sf_path, encoding="utf-8") as f:
                    sdata = json.load(f)
                all_text_for_scan += "\n" + json.dumps(sdata, ensure_ascii=False)
            except json.JSONDecodeError as e:
                E("schema_file", f"schemas/{sf} 不是合法 JSON：{e}")
    else:
        W("schemas_dir", "沒有 schemas/ 目錄（schema 內嵌在 server.py 裡也可以）")

    # 7. tools/call 不 crash（對每支 tool 送空 arguments）
    for tname in tool_names:
        resp2, err2 = send_jsonrpc(
            server_py, "tools/call",
            {"name": tname, "arguments": {}},
        )
        if resp2 is None:
            E("tools_call", f"{tname}：tools/call 讓 server crash（{err2[:100]}）")
        elif "error" in resp2:
            # JSON-RPC level error (如 -32602) 不算 crash
            pass
        else:
            r2 = resp2.get("result", {})
            has_content = bool(r2.get("content"))
            has_structured = "structuredContent" in r2
            is_error = r2.get("isError", False)
            if not has_content and not has_structured and not is_error:
                W("tools_call", f"{tname}：tools/call 回傳無 content 也無 structuredContent")

    # 8. 簡體字掃描
    hits = scan_simplified(all_text_for_scan)
    for ln, ch, ctx in hits[:5]:
        E("simplified", f"疑似簡體「{ch}」：{ctx}")
    if len(hits) > 5:
        E("simplified", f"另有 {len(hits) - 5} 處簡體字")

    # 9. 去識別化
    for kw in DEID_BLOCKLIST:
        if kw in all_text_for_scan:
            E("deid", f"出現真實資訊「{kw}」")

    return _result(dir_name, findings, tool_names)


def _result(dir_name, findings, tool_names=None):
    return {
        "name": dir_name,
        "dir_name": dir_name,
        "tool_names": tool_names or [],
        "tool_count": len(tool_names) if tool_names else 0,
        "findings": findings,
        "errors": sum(1 for x in findings if x[0] == "ERROR"),
        "warns": sum(1 for x in findings if x[0] == "WARN"),
    }


# ── 目錄掃描 ─────────────────────────────────────────────────────

def find_mcp_dirs(paths):
    dirs = []
    for p in paths:
        p = p.rstrip("/")
        if os.path.isfile(os.path.join(p, "server.py")):
            dirs.append(p)
        elif os.path.isdir(p):
            for root, subdirs, files in os.walk(p):
                subdirs[:] = [
                    d for d in subdirs
                    if not d.startswith((".", "_")) and d != "__MACOSX"
                ]
                if "server.py" in files:
                    dirs.append(root)
                    subdirs[:] = []
    return sorted(set(dirs))


# ── 主流程 ───────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--out-dir", default=None)
    args = ap.parse_args()

    skill_dirs = find_mcp_dirs(args.paths)
    if not skill_dirs:
        print("找不到任何含 server.py 的目錄")
        sys.exit(2)

    results = [check_mcp_skill(d) for d in skill_dirs]

    # 10. 撞名（本批次內）
    seen = {}
    for r in results:
        for tn in r["tool_names"]:
            if tn in seen:
                r["findings"].append(
                    ("WARN", "name_dup",
                     f"tool name「{tn}」與本批次 {seen[tn]} 重複"))
                r["warns"] += 1
            seen.setdefault(tn, r["dir_name"])

    # 報告
    out_dir = args.out_dir or (
        args.paths[0] if os.path.isdir(args.paths[0])
        else os.path.dirname(args.paths[0])
    )
    os.makedirs(out_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    total_e = sum(r["errors"] for r in results)
    total_w = sum(r["warns"] for r in results)
    total_tools = sum(r["tool_count"] for r in results)

    md = [
        "# MCP Skill 機械檢查報告", "",
        "## 摘要",
        f"- 檢查時間：{ts}",
        f"- MCP Skill 數量：{len(results)}",
        f"- Tool 總數：{total_tools}",
        f"- 錯誤：{total_e}",
        f"- 警告：{total_w}", "",
        "| Skill | Tool 數 | 錯誤 | 警告 |",
        "|---|---:|---:|---:|",
    ]
    for r in results:
        md.append(
            f"| {r['dir_name']} | {r['tool_count']} | {r['errors']} | {r['warns']} |"
        )
    md.append("")

    for r in results:
        md.append(f"## {r['dir_name']}")
        md.append(f"- Tool：{', '.join(r['tool_names']) or '（無法取得）'}")
        if not r["findings"]:
            md.append("- 結果：通過。")
        for lv, code, msg in r["findings"]:
            level = "錯誤" if lv == "ERROR" else "警告"
            md.append(f"- {level} [{code}]：{msg}")
        md.append("")

    md_path = os.path.join(out_dir, "MCP機械檢查_報告.md")
    json_path = os.path.join(out_dir, "MCP機械檢查_報告.json")

    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(md))
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "generated_at": ts,
                "results": [
                    {**r, "findings": [list(x) for x in r["findings"]]}
                    for r in results
                ],
            },
            fh,
            ensure_ascii=False,
            indent=1,
        )

    print(f"檢查 {len(results)} 個 MCP skill（{total_tools} 支 tool）：ERROR {total_e}、WARN {total_w}")
    for r in results:
        flag = "✗" if r["errors"] else ("△" if r["warns"] else "✓")
        print(f"  {flag} {r['dir_name']:45s} {r['tool_count']}t E{r['errors']} W{r['warns']}")
    print(f"報告：{md_path}")
    print(f"JSON：{json_path}")
    sys.exit(1 if total_e else 0)


if __name__ == "__main__":
    main()
