"""Shared fixtures: every test runs against its own temp ``HOUSTON_HOME``; the real store is never touched."""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))


def option(label: str, star: bool = False) -> dict[str, Any]:
    """Return a complete, valid option."""
    o: dict[str, Any] = {
        "label": label,
        "then": f"{label}: what happens next in one line",
        "means": f"You choose {label}.",
        "next_steps": [f"Claude does {label}", "Claude reports back"],
        "cost": "$0, 5 minutes",
        "risk": "Small; easy to undo.",
    }
    if star:
        o["star"] = True
        o["why_pick"] = f"{label} is the cheapest safe path."
    return o


BASE: dict[str, Any] = {
    "thread": {"slug": "api-v2", "name": "API v2 rollout"},
    "kind": "go_ahead",
    "title": "Merge API v2 (PR #45)?",
    "question": "Who reviews PR #45 so the API v2 change can merge?",
    "options": [option("merge now"), option("ask Bob", star=True), option("wait for Alice")],
    "background": [
        "API v2 is the new public endpoint set.",
        "It cuts p95 latency by 30%.",
        "PR #45 is open with green CI.",
    ],
    "blocks": ["mobile app switch to API v2"],
    "source": {"session_id": "sess-a", "cwd": "/tmp", "branch": "feat/x", "transcript": "", "pointer": "PR #45"},
}


@pytest.fixture
def make() -> Any:
    """Return a factory for valid decision inputs with overrides."""

    def _make(**over: Any) -> dict[str, Any]:
        """Copy the base decision and apply overrides."""
        d = copy.deepcopy(BASE)
        d.update(over)
        return d

    return _make


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the store at a temp dir and clear session env, for every test."""
    h = tmp_path / "decisions"
    monkeypatch.setenv("HOUSTON_HOME", str(h))
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("HOUSTON_HOOKS_OFF", raising=False)
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    monkeypatch.delenv("HOUSTON_USER_NAME", raising=False)
    return h


def write_transcript(path: Path, turns: list[tuple[str, str, str]]) -> Path:
    """Write a minimal transcript jsonl: (who, text, iso timestamp) per turn, plus noise lines."""
    lines = [{"type": "queue-operation", "operation": "x"}]
    for who, text, ts in turns:
        if who == "user":
            lines.append(
                {
                    "type": "user",
                    "origin": {"kind": "human"},
                    "timestamp": ts,
                    "message": {"role": "user", "content": text},
                }
            )
        elif who == "tool":
            lines.append(
                {
                    "type": "assistant",
                    "timestamp": ts,
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": text}}],
                    },
                }
            )
            lines.append(
                {
                    "type": "user",
                    "timestamp": ts,
                    "message": {
                        "role": "user",
                        "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}],
                    },
                }
            )
        else:
            lines.append(
                {
                    "type": "assistant",
                    "timestamp": ts,
                    "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
                }
            )
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\nnot json\n")
    return path
