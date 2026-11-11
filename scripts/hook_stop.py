"""Stop hook: catch a reply that asks the user to decide without logging the decision in the inbox.

Conservative and cheap: a regex check on the last assistant message (``last_assistant_message`` on stdin,
else the transcript tail), and a scan of the transcript tail for a ``houston`` add/update/--same-as call since
the last human prompt. ``HOUSTON_STOP_HOOK=off`` turns only this hook off. Blocks at most once per reply: never when ``stop_hook_active`` is true, and never twice
for the same message text. Any error exits 0 at once (the stop goes through). ``HOUSTON_HOOKS_OFF=1`` disables it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

TAIL_BYTES = 3_000_000  # read only the end of large transcripts
ASK_PATTERNS = [
    r"\byour call\b",
    r"\bsay ['\"“]?\d\b",
    r"\bsay ['\"“][a-z][^'\"”]{0,20}['\"”]",
    r"\bwhich (?:one )?do you want\b",
    r"\bwhich (?:option|would you prefer)\b",
    r"\bpending on you\b",
    r"\bwaits? on you\b",
    r"\bopen decisions? (?:in|are in|listed in)\b",
    r"^\s*\**later:\**\s",
    r"\bneeds? your (?:ok|go-ahead|approval|call|decision)\b",
    r"\bapprove (?:the )?(?:spend|cost|budget)\b",
    r"\b(?:can|could) you (?:tell|ask|ping) (?!me\b|us\b|claude\b)\w+",
]
ASK_RE = re.compile("|".join(ASK_PATTERNS), re.IGNORECASE | re.MULTILINE)
INBOX_LINE_RE = re.compile(r"decisions? (?:is|are) in the inbox", re.IGNORECASE)
HOUSTON_CALL_RE = re.compile(r"houston(?:\.py)?\s+(?:add|update)\b|--same-as\b")
sys.path.insert(0, str(Path(__file__).resolve().parent))


def reason() -> str:
    """The block message, naming the user and the CLI the way this install can run it."""
    import store

    who, cli = store.user_name(), store.cli_command()
    return (
        f"Your reply asks {who} to decide something, but no `houston add` ran this turn. Log each decision in "
        f"{store.possessive(who)} inbox before you stop: write the JSON (thread, kind, title, question, options with "
        "then/means/next_steps/cost/risk, one star with why_pick, background, blocks) and pipe it to "
        f"`{cli} add` (`{cli} add --example` prints a valid input; `{cli} threads` lists thread slugs to reuse). "
        "Then end the reply with 'N decisions are in the inbox: run `houston`' instead of a footer list. "
        f"If nothing in the reply waits on {who}, stop again without changes."
    )


def read_tail(path: str) -> list[dict[str, Any]]:
    """Parse the last ``TAIL_BYTES`` of a transcript into JSON rows (broken lines skipped)."""
    p = Path(path).expanduser()
    if not path or not p.is_file():
        return []
    size = p.stat().st_size
    with open(p, "rb") as fh:
        if size > TAIL_BYTES:
            fh.seek(size - TAIL_BYTES)
            fh.readline()  # drop the partial first line
        data = fh.read().decode("utf-8", errors="replace")
    rows = []
    for line in data.splitlines():
        if '"user"' not in line and '"assistant"' not in line:  # cheap prefilter before json.loads
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def is_human(row: dict[str, Any]) -> bool:
    """True for a prompt the user typed (not a tool result, notice or peer message)."""
    if row.get("type") != "user" or row.get("isMeta") or row.get("isSidechain"):
        return False
    origin = row.get("origin")
    if isinstance(origin, dict):
        return origin.get("kind") == "human"
    content = (row.get("message") or {}).get("content")
    if isinstance(content, list):
        return not any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
    return isinstance(content, str) and not content.lstrip().startswith("<")


def since_last_human(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rows after the last human prompt (all rows when none is in the tail)."""
    for i in range(len(rows) - 1, -1, -1):
        if is_human(rows[i]):
            return rows[i + 1 :]
    return rows


def logged_this_turn(turn: list[dict[str, Any]]) -> bool:
    """True when a Bash call in this turn ran houston add / update / --same-as."""
    for row in turn:
        if row.get("type") != "assistant":
            continue
        for b in (row.get("message") or {}).get("content") or []:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                cmd = str((b.get("input") or {}).get("command", ""))
                if HOUSTON_CALL_RE.search(cmd):
                    return True
    return False


def last_text(turn: list[dict[str, Any]]) -> str:
    """Text of the last assistant message with prose in it."""
    for row in reversed(turn):
        if row.get("type") == "assistant":
            parts = [
                b.get("text", "")
                for b in (row.get("message") or {}).get("content") or []
                if isinstance(b, dict) and b.get("type") == "text"
            ]
            if any(p.strip() for p in parts):
                return "\n".join(parts)
    return ""


def state_file(session_id: str) -> Path:
    """Where the last blocked message hash for a session is kept."""
    env = os.environ.get("HOUSTON_HOME")
    root = Path(env).expanduser() if env else Path.home() / ".claude" / "houston"
    safe = re.sub(r"[^A-Za-z0-9_-]", "", session_id) or "unknown"
    return root / "hookstate" / f"{safe}.stop"


def check(payload: dict[str, Any]) -> str | None:
    """Return the block reason, or None to let the stop go through."""
    if payload.get("stop_hook_active"):
        return None
    rows = read_tail(str(payload.get("transcript_path") or ""))
    turn = since_last_human(rows)
    msg = payload.get("last_assistant_message")
    if not isinstance(msg, str) or not msg.strip():
        msg = last_text(turn)
    if not msg or not ASK_RE.search(msg) or INBOX_LINE_RE.search(msg):
        return None
    if logged_this_turn(turn):
        return None
    digest = hashlib.sha1(msg.encode("utf-8", errors="replace")).hexdigest()
    sf = state_file(str(payload.get("session_id") or ""))
    if sf.exists() and sf.read_text().strip() == digest:
        return None  # already blocked this exact reply once
    sf.parent.mkdir(parents=True, exist_ok=True)
    sf.write_text(digest)
    return reason()


def main() -> int:
    """Read stdin; print a block decision or nothing; always exit 0."""
    if os.environ.get("HOUSTON_HOOKS_OFF") or os.environ.get("HOUSTON_STOP_HOOK", "").lower() in ("off", "0", "false"):
        return 0
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        if not isinstance(payload, dict):
            return 0
        reason = check(payload)
        if reason:
            print(json.dumps({"decision": "block", "reason": reason}))
    except Exception:  # noqa: BLE001 - a hook must never trap the session
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
