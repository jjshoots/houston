"""SessionStart + UserPromptSubmit hook: put the user's undelivered answers for this session into context.

Reads the hook JSON on stdin (``session_id``, ``cwd``, ``hook_event_name``). Prints plain text on stdout
(Claude Code adds it to context) only when there is something to say; otherwise prints nothing.
Answers stay undelivered until the session runs ``answers --mark-delivered``, so they repeat until acted on.
Never fails the prompt: any error exits 0 silently. ``HOUSTON_HOOKS_OFF=1`` disables it.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

MAX_CHARS = 2000
MAX_ROWS = 6


def build(payload: dict[str, object]) -> str:
    """Return the context text for one hook call ('' when there is nothing)."""
    import store

    sid = str(payload.get("session_id") or "")
    if not sid or not store.items_dir().exists():
        return ""
    items = store.all_items()
    mine = [i for i in items if sid in i.get("sessions", [])]
    threads = {i["thread"] for i in mine}
    rows = store.pending_answers(session=sid)
    seen = {(r["id"], r["index"]) for r in rows}
    others = [r for t in sorted(threads) for r in store.pending_answers(thread=t) if (r["id"], r["index"]) not in seen]
    n_open = sum(1 for i in mine if store.effective_state(i) in store.OPEN_STATES)
    if not rows and not others and not n_open:
        return ""
    cli = store.cli_command()
    who = store.user_name()
    out = []
    if rows or others:
        out.append(f"[houston] {len(rows) + len(others)} answer(s) from {who} are waiting (decision inbox):")
        for r in (rows + others)[:MAX_ROWS]:
            tag = "" if (r["id"], r["index"]) in seen else " (asked by another session of this thread)"
            out.append(f"- {r['id']} {r['title']} → {r['text']}{tag}")
        if len(rows) + len(others) > MAX_ROWS:
            out.append(f"- … and {len(rows) + len(others) - MAX_ROWS} more")
        out.append(
            f"Take them with `{cli} answers --session {sid} --mark-delivered` (add `--thread <slug>` for the "
            "others), then act on each."
        )
    if n_open:
        out.append(f"[houston] This session has {n_open} decision(s) open in {store.possessive(who)} inbox.")
    text = "\n".join(out)
    return text if len(text) <= MAX_CHARS else text[: MAX_CHARS - 1] + "…"


def main() -> int:
    """Read stdin, print context, always exit 0."""
    if os.environ.get("HOUSTON_HOOKS_OFF"):
        return 0
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        if not isinstance(payload, dict):
            return 0
        text = build(payload)
        if text:
            print(text)
    except Exception:  # noqa: BLE001 - a hook must never break the session
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
