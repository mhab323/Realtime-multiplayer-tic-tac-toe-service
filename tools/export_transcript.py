"""Turn a Claude Code session log (.jsonl) into readable Markdown.

The raw log is one JSON object per line and is not something a reviewer should
be asked to read. This flattens it to a conversation: what was asked, what was
answered, which tools ran with which arguments, and what came back — with long
payloads truncated so the shape of the work stays visible.

    python tools/export_transcript.py <session.jsonl> -o transcript/session.md

Session logs live in ~/.claude/projects/<slugified-cwd>/<session-id>.jsonl
Run with --list to see them, newest first.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

PROJECTS = Path.home() / ".claude" / "projects"

INPUT_LIMIT = 900   # chars of a tool's arguments to keep
OUTPUT_LIMIT = 600  # chars of a tool's result to keep


def clip(text: str, limit: int) -> str:
    text = text.rstrip()
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n… [{len(text) - limit:,} more characters]"


def as_text(content) -> str:
    """Tool results arrive as a string, or a list of content blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            block.get("text", "") if isinstance(block, dict) else str(block)
            for block in content
        )
    return str(content)


def stamp(entry: dict) -> str:
    raw = entry.get("timestamp")
    if not raw:
        return ""
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).strftime("%H:%M:%S")
    except ValueError:
        return ""


def render(entry: dict, out: list[str]) -> None:
    message = entry.get("message")
    if not isinstance(message, dict):
        return

    role = message.get("role")
    content = message.get("content")
    blocks = content if isinstance(content, list) else [{"type": "text", "text": content}]

    # A "user" entry carrying tool_result blocks is the harness replying to a
    # tool call, not the person typing. Label it as such.
    is_tool_reply = role == "user" and any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in blocks
    )

    for block in blocks:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")

        if kind == "text":
            text = (block.get("text") or "").strip()
            if not text:
                continue
            who = "User" if role == "user" else "Claude"
            out.append(f"\n### {who} · {stamp(entry)}\n\n{text}\n")

        elif kind == "thinking":
            thought = (block.get("thinking") or "").strip()
            if thought:
                out.append(
                    f"\n<details><summary>Claude's reasoning</summary>\n\n"
                    f"{clip(thought, 4000)}\n\n</details>\n"
                )

        elif kind == "tool_use":
            args = json.dumps(block.get("input", {}), indent=2, ensure_ascii=False)
            out.append(
                f"\n**→ {block.get('name', '?')}**\n\n"
                f"```json\n{clip(args, INPUT_LIMIT)}\n```\n"
            )

        elif kind == "tool_result" and is_tool_reply:
            body = clip(as_text(block.get("content", "")), OUTPUT_LIMIT)
            if body.strip():
                out.append(f"\n```\n{body}\n```\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session", nargs="?", help="path to a .jsonl session log")
    parser.add_argument("-o", "--out", default="transcript/session.md")
    parser.add_argument("--list", action="store_true", help="list session logs")
    args = parser.parse_args()

    if args.list or not args.session:
        logs = sorted(PROJECTS.glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not logs:
            print(f"no session logs under {PROJECTS}")
            return
        print(f"session logs under {PROJECTS}, newest first:\n")
        for log in logs[:15]:
            when = datetime.fromtimestamp(log.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
            print(f"  {when}  {log.stat().st_size / 1024:8,.0f} KB  {log}")
        return

    source = Path(args.session)
    out: list[str] = [
        f"# Agent transcript\n",
        f"\nExported from `{source.name}` on "
        f"{datetime.now().strftime('%Y-%m-%d %H:%M')}. "
        f"Tool arguments are clipped at {INPUT_LIMIT} characters and results at "
        f"{OUTPUT_LIMIT}, so long file writes and command output show their shape "
        f"rather than their full contents.\n",
    ]

    skipped = 0
    with source.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                render(json.loads(line), out)
            except json.JSONDecodeError:
                skipped += 1

    destination = Path(args.out)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("".join(out), encoding="utf-8")

    size = destination.stat().st_size
    print(f"wrote {destination} ({size / 1024:,.0f} KB)")
    if skipped:
        print(f"skipped {skipped} unparseable line(s)")


if __name__ == "__main__":
    main()
