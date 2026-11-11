"""Answer the user's "?" question about one decision with a headless, tool-less Claude (``claude -p``).

Flags were checked against ``claude --help`` (Claude Code 2.1.322):
``-p`` print mode, ``--model sonnet``, ``--tools ""`` (no tools), ``--no-session-persistence``,
``--disable-slash-commands``, ``--strict-mcp-config`` (no MCP servers), ``--output-format text``.
The prompt goes on stdin. ``HOUSTON_HOOKS_OFF=1`` keeps the houston hooks quiet inside that run.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import store  # noqa: E402

TIMEOUT_S = 120
FLAGS = [
    "-p",
    "--model",
    "sonnet",
    "--tools",
    "",
    "--no-session-persistence",
    "--disable-slash-commands",
    "--strict-mcp-config",
    "--output-format",
    "text",
]
PROMPT = """You help {who} decide one item in their decision inbox. Answer the question about it.
Rules: plain words, short sentences, active voice. At most 120 words. Define any code name in 3-6 words.
Use only the facts below; if they do not answer the question, say what is missing and who knows it.
Do not pick for them unless they ask; Claude's star and its reason are in the data.

DECISION (JSON):
{decision}

SOURCE CONVERSATION AROUND THE ASK ({where}):
{context}

EARLIER QUESTIONS ON THIS DECISION:
{history}

THE QUESTION: {question}
"""


def build_prompt(item: dict[str, Any], where: str, turns: list[list[str]], question: str) -> str:
    """Return the full prompt text for one question."""
    keep = {
        k: item.get(k)
        for k in (
            "title",
            "question",
            "kind",
            "options",
            "background",
            "blocks",
            "changed_since",
            "state",
            "times_asked",
        )
    }
    keep["source"] = {k: (item.get("source") or {}).get(k) for k in ("pointer", "branch", "date")}
    ctx = "\n".join(f"{who}: {text}" for who, text in turns)[-12000:] or "(none)"
    hist = "\n".join(f"Q: {q['q']}\nA: {q['a']}" for q in item.get("qa", [])) or "(none)"
    return PROMPT.format(
        decision=json.dumps(keep, ensure_ascii=False, indent=1),
        where=where,
        context=ctx,
        history=hist,
        question=question,
        who=store.user_name(),
    )


def ask_claude(prompt: str, cwd: str | None = None, timeout: float = TIMEOUT_S) -> str:
    """Run the headless model; return its answer, or one clear line saying why there is none (never raises)."""
    binary = os.environ.get("HOUSTON_CLAUDE_BIN") or shutil.which("claude")
    if not binary:
        return "(no answer: the `claude` command is not on PATH)"
    run_dir = cwd if cwd and os.path.isdir(cwd) else os.path.expanduser("~")
    env = {**os.environ, "HOUSTON_HOOKS_OFF": "1"}
    try:
        r = subprocess.run(
            [binary, *FLAGS],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=run_dir,
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return f"(no answer: Claude did not reply in {int(timeout)} s; try again or press c for the source)"
    except OSError as e:
        return f"(no answer: could not start claude: {e})"
    out = r.stdout.strip()
    if r.returncode != 0 or not out:
        err = (r.stderr.strip().splitlines() or ["no output"])[-1][:200]
        return f"(no answer: claude exited {r.returncode}: {err})"
    return out
