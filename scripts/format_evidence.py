#!/usr/bin/env python3
"""格式化實測存證，保留原始回應並提供可讀的 JSON 回應欄位。

用法：
  python3 scripts/format_evidence.py <驗證結果.json 或報告目錄> [...]
"""

import json
import sys
from pathlib import Path


def parse_response(raw: object) -> object | None:
    """只接受一份完整 JSON，避免把截斷或夾雜文字的輸出誤標為可讀回應。"""
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    try:
        value, end = json.JSONDecoder().raw_decode(text)
    except json.JSONDecodeError:
        return None
    return value if not text[end:].strip() else None


def contains_replacement_character(value: object) -> bool:
    if isinstance(value, str):
        return "\ufffd" in value
    if isinstance(value, dict):
        return any(contains_replacement_character(key) or contains_replacement_character(item)
                   for key, item in value.items())
    if isinstance(value, list):
        return any(contains_replacement_character(item) for item in value)
    return False


def evidence_files(paths: list[str]) -> list[Path]:
    found: set[Path] = set()
    for raw_path in paths:
        path = Path(raw_path)
        if path.is_file() and path.name.endswith("_驗證結果.json"):
            found.add(path)
        elif path.is_dir():
            found.update(path.glob("*_驗證結果.json"))
    return sorted(found)


def format_file(path: Path) -> tuple[bool, str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return False, f"無法讀取：{exc}"
    if not isinstance(data, list):
        return False, "頂層必須是案例陣列"
    if contains_replacement_character(data):
        return False, "含有 Unicode 替代字元（�），來源文字已損壞，未改寫"

    rendered = 0
    for case in data:
        if not isinstance(case, dict):
            continue
        response = parse_response(case.get("raw_response"))
        if response is not None:
            case["response"] = response
            rendered += 1
        else:
            case.pop("response", None)

    output = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    try:
        original = path.read_text(encoding="utf-8")
        if original != output:
            path.write_text(output, encoding="utf-8")
    except OSError as exc:
        return False, f"無法寫入：{exc}"
    return True, f"{len(data)} 案，其中 {rendered} 案已建立 response 可讀欄位"


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__.strip())
        return 2
    files = evidence_files(sys.argv[1:])
    if not files:
        print("找不到 *_驗證結果.json")
        return 2
    failed = 0
    for path in files:
        ok, message = format_file(path)
        print(f"{'完成' if ok else '失敗'}：{path}｜{message}")
        failed += not ok
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
