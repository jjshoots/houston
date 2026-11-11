"""CLI round trip through real subprocesses: add -> list -> show -> answers -> mark-delivered -> close."""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Any

import store
from conftest import SCRIPTS

CLI = [sys.executable, str(SCRIPTS / "houston.py")]


def run(*args: str, stdin: str = "") -> subprocess.CompletedProcess[str]:
    """Run the CLI with the test's HOUSTON_HOME (inherited from the environment)."""
    return subprocess.run([*CLI, *args], input=stdin, capture_output=True, text=True, timeout=30, check=False)


def test_round_trip(make: Any) -> None:
    """Add, dedupe, list, answer, deliver once, then close."""
    r = run("add", "--json", stdin=json.dumps(make()))
    assert r.returncode == 0, r.stderr
    did = json.loads(r.stdout)["id"]
    r = run("add", stdin=json.dumps(make()))
    assert r.returncode == 0 and f"already in the inbox as {did}" in r.stdout
    r = run("list")
    assert did in r.stdout and "x2" in r.stdout
    r = run("list", "--json", "--session", "sess-a")
    assert [i["id"] for i in json.loads(r.stdout)] == [did]
    assert "ask Bob" in run("show", did).stdout
    r = run("threads")
    assert "api-v2" in r.stdout and "1 pending" in r.stdout
    assert run("thread", "set", "api-v2", "--goal", "Ship API v2", "--state", "blocked on you").returncode == 0
    assert store.threads()["api-v2"]["goal"] == "Ship API v2"

    assert "No new answers." in run("answers", "--session", "sess-a").stdout
    store.submit_batch({did: {"kind": "you_pick", "option": 2, "label": "ask Bob"}})
    r = run("answers", "--session", "sess-a", "--mark-delivered")
    assert "you (Claude) pick" in r.stdout and "marked 1 delivered" in r.stdout
    assert "No new answers." in run("answers", "--session", "sess-a").stdout
    assert "No new answers." in run("answers", "--session", "someone-else").stdout

    assert run("close", did, "--reason", "moot: merged by Alice").returncode == 0
    assert "closed" in run("list", "--state", "closed").stdout
    assert run("doctor").returncode == 0


def test_validation_errors_are_listed(make: Any) -> None:
    """Bad input exits 2 and prints each problem on its own line."""
    bad = make(options=[{"label": "a"}])
    r = run("add", stdin=json.dumps(bad))
    assert r.returncode == 2
    assert "options[1].then" in r.stderr and "star exactly one" in r.stderr
    r = run("add", stdin="{not json")
    assert r.returncode == 2 and "not valid JSON" in r.stderr
    assert run("show", "nope").returncode == 2
    assert run("answers").returncode == 2, "answers needs a scope"


def test_example_is_valid(monkeypatch: Any) -> None:
    """The printed example passes validation as is (with a session id from the env)."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "ex")
    ex = run("add", "--example").stdout
    r = run("add", stdin=ex)
    assert r.returncode == 0 and r.stdout.startswith("added ")


def test_update_and_same_as(make: Any) -> None:
    """Update from a file; --same-as with empty stdin re-asks."""
    did = json.loads(run("add", "--json", stdin=json.dumps(make())).stdout)["id"]
    store.submit_batch({did: {"kind": "explain", "note": "what is API v2"}})
    r = run("update", did, stdin=json.dumps({"question": "Who reviews PR #45 (the API v2 change) today?"}))
    assert r.returncode == 0 and "state pending" in r.stdout
    r = run("add", "--same-as", did, "--json")
    assert r.returncode == 0 and json.loads(r.stdout)["times_asked"] == 2
