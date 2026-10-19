#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# ///
"""Token cost of Claude Code sessions, from their transcript .jsonl files.

Scores a skill change: run it on the session that used the skill, before and
after. Prints turns, cache-read/creation tokens, tool-result chars by tool, and
the largest tool results. Images count as ~6000 chars (~1.5k tokens).

    tools/session_cost.py ~/.claude/projects/-Users-sprague-Documents-Notes/<id>.jsonl
"""

import collections
import json
import sys
from pathlib import Path

IMAGE_CHARS = 6000
USAGE_KEYS = ("cache_read_input_tokens", "cache_creation_input_tokens",
              "input_tokens", "output_tokens")


def result_chars(content) -> int:
    if not isinstance(content, list):
        return len(str(content))
    sizes = {"text": lambda x: len(x.get("text", "")), "image": lambda x: IMAGE_CHARS}
    return sum(sizes.get(x.get("type"), lambda x: len(json.dumps(x)))(x)
               for x in content if isinstance(x, dict))


def label(inp: dict) -> str:
    s = inp.get("command") or inp.get("file_path") or inp.get("skill") or json.dumps(inp)
    return str(s).replace("\n", " ")[:110]


def audit(path: Path, top: int) -> None:
    calls, rows = {}, []
    usage = collections.Counter()
    turns = 0
    for line in path.open():
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        m = d.get("message") or {}
        content = m.get("content")
        if d.get("type") == "assistant":
            turns += 1
            for k in USAGE_KEYS:
                usage[k] += (m.get("usage") or {}).get(k) or 0
            for c in content or []:
                if isinstance(c, dict) and c.get("type") == "tool_use":
                    calls[c["id"]] = (c["name"], label(c.get("input", {})))
        elif d.get("type") == "user" and isinstance(content, list):
            for c in content:
                if isinstance(c, dict) and c.get("type") == "tool_result":
                    name, desc = calls.get(c["tool_use_id"], ("?", "?"))
                    rows.append((result_chars(c.get("content")), name, desc))

    by_tool = collections.Counter()
    for n, name, _ in rows:
        by_tool[name] += n
    print(f"## {path.name}")
    print(f"turns {turns}  cache_read {usage['cache_read_input_tokens']:,}  "
          f"cache_create {usage['cache_creation_input_tokens']:,}  "
          f"output {usage['output_tokens']:,}  tool_chars {sum(by_tool.values()):,}")
    for name, n in by_tool.most_common():
        print(f"  {n:>8,}  {name}")
    print("largest results:")
    for n, name, desc in sorted(rows, reverse=True)[:top]:
        print(f"  {n:>8,}  {name[:24]:24} {desc}")
    print()


def main(argv: list[str]) -> int:
    top = 15
    if "--top" in argv:
        i = argv.index("--top")
        top = int(argv[i + 1])
        del argv[i:i + 2]
    if not argv:
        print(__doc__)
        return 2
    for p in argv:
        audit(Path(p).expanduser(), top)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
