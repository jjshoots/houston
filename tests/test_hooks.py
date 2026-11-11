"""Hook scripts fed sample hook JSON on stdin, run as real subprocesses."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import store
from conftest import SCRIPTS, write_transcript


def hook(
    name: str, payload: Any, raw: str | None = None, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run one hook script with stdin."""
    text = raw if raw is not None else json.dumps(payload)
    return subprocess.run(
        [sys.executable, str(SCRIPTS / name)],
        input=text,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
        env={**os.environ, **(env or {})},
    )


def start_payload(sid: str = "sess-a", event: str = "SessionStart") -> dict[str, Any]:
    """A SessionStart / UserPromptSubmit stdin payload."""
    return {
        "session_id": sid,
        "transcript_path": "/nope.jsonl",
        "cwd": "/tmp",
        "hook_event_name": event,
        "source": "startup",
        "prompt": "hi",
    }


def test_context_hook_silent_when_empty() -> None:
    """No store, no output."""
    r = hook("hook_context.py", start_payload())
    assert r.returncode == 0 and r.stdout == ""


def test_context_hook_prints_answers_and_count(make: Any) -> None:
    """Undelivered answers and the open count appear; other sessions see nothing."""
    a, _, _ = store.add(make())
    store.add(make(title="Spend $40 on a load test?", question="Approve $40 for the load test?"))
    store.submit_batch({a: {"kind": "option", "option": 2, "label": "ask Bob", "note": "ping him"}})
    for event in ("SessionStart", "UserPromptSubmit"):
        r = hook("hook_context.py", start_payload(event=event))
        assert r.returncode == 0
        assert "1 answer(s) from the user" in r.stdout and "picked 2 ask Bob" in r.stdout
        assert "--mark-delivered" in r.stdout and "1 decision(s) open" in r.stdout
        assert len(r.stdout) < 2100
    assert hook("hook_context.py", start_payload(sid="stranger")).stdout == ""
    store.mark_delivered(store.pending_answers(session="sess-a"), "sess-a")
    r = hook("hook_context.py", start_payload())
    assert "answer(s)" not in r.stdout and "1 decision(s) open" in r.stdout


@pytest.mark.parametrize("raw", ["", "not json", "[1, 2]", '{"session_id": 5}', "{"])
@pytest.mark.parametrize("name", ["hook_context.py", "hook_stop.py"])
def test_hooks_survive_malformed_input(name: str, raw: str) -> None:
    """Malformed stdin: exit 0, no output."""
    r = hook(name, None, raw=raw)
    assert r.returncode == 0 and r.stdout == "" and r.stderr == ""


def stop_payload(tr: Path, msg: str | None, active: bool = False) -> dict[str, Any]:
    """A Stop stdin payload."""
    p: dict[str, Any] = {
        "session_id": "sess-a",
        "transcript_path": str(tr),
        "cwd": "/tmp",
        "hook_event_name": "Stop",
        "stop_hook_active": active,
    }
    if msg is not None:
        p["last_assistant_message"] = msg
    return p


ASKING = "PR #45 is open.\n\n**Your call:** 1 merge now, 2 ask Bob ★. Say 1 or 2."


def test_stop_blocks_unlogged_ask_once(tmp_path: Path) -> None:
    """An ask with no houston call this turn blocks once; the same reply never blocks twice."""
    tr = write_transcript(
        tmp_path / "t.jsonl",
        [("user", "status?", "2026-11-11T10:00:00Z"), ("assistant", ASKING, "2026-11-11T10:01:00Z")],
    )
    r = hook("hook_stop.py", stop_payload(tr, ASKING))
    out = json.loads(r.stdout)
    assert r.returncode == 0 and out["decision"] == "block" and "houston.py add" in out["reason"]
    again = hook("hook_stop.py", stop_payload(tr, ASKING))
    assert again.stdout == "", "never blocks the same reply twice"
    assert hook("hook_stop.py", stop_payload(tr, ASKING + " ", active=True)).stdout == ""


def test_stop_reads_message_from_transcript_when_missing(tmp_path: Path) -> None:
    """Without last_assistant_message the transcript's last prose turn is checked."""
    tr = write_transcript(
        tmp_path / "t.jsonl",
        [("user", "go", "2026-11-11T10:00:00Z"), ("assistant", "Which do you want: A or B?", "2026-11-11T10:01:00Z")],
    )
    assert json.loads(hook("hook_stop.py", stop_payload(tr, None)).stdout)["decision"] == "block"


def test_stop_allows_when_logged_or_not_asking(tmp_path: Path) -> None:
    """A houston add since the last human prompt, the inbox line, or a plain reply: no block."""
    logged = write_transcript(
        tmp_path / "a.jsonl",
        [
            ("user", "status?", "2026-11-11T10:00:00Z"),
            ("tool", "houston add < /tmp/d.json", "2026-11-11T10:00:30Z"),
            ("assistant", ASKING, "2026-11-11T10:01:00Z"),
        ],
    )
    assert hook("hook_stop.py", stop_payload(logged, ASKING)).stdout == ""
    old = write_transcript(
        tmp_path / "b.jsonl",
        [
            ("tool", "python3 houston.py add < /tmp/d.json", "2026-11-11T09:00:00Z"),
            ("user", "and now?", "2026-11-11T10:00:00Z"),
            ("assistant", ASKING, "2026-11-11T10:01:00Z"),
        ],
    )
    assert (
        json.loads(hook("hook_stop.py", stop_payload(old, ASKING)).stdout)["decision"] == "block"
    ), "a houston call before the last human prompt does not count"
    inbox = "Done. 2 decisions are in the inbox: run `houston`. Your call is there."
    assert hook("hook_stop.py", stop_payload(old, inbox)).stdout == ""
    assert hook("hook_stop.py", stop_payload(old, "Tests pass. Commit abc123.")).stdout == ""
    assert hook("hook_stop.py", stop_payload(tmp_path / "missing.jsonl", "All done.")).stdout == ""


def test_hooks_off_switch(make: Any) -> None:
    """HOUSTON_HOOKS_OFF silences both hooks (used inside the headless '?' run)."""
    a, _, _ = store.add(make())
    store.submit_batch({a: {"kind": "drop"}})
    assert hook("hook_context.py", start_payload(), env={"HOUSTON_HOOKS_OFF": "1"}).stdout == ""


def test_stop_hook_off_switch(tmp_path: Path) -> None:
    """HOUSTON_STOP_HOOK=off lets every stop through."""
    tr = write_transcript(tmp_path / "t.jsonl", [("user", "status?", "2026-11-11T10:00:00Z")])
    assert hook("hook_stop.py", stop_payload(tr, ASKING), env={"HOUSTON_STOP_HOOK": "off"}).stdout == ""
