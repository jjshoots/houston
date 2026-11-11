"""End-to-end smoke: 6 realistic decisions in 2 threads via the CLI -> stage and submit in the inbox -> answers.

Set ``HOUSTON_SCREENSHOT=<dir>`` to write ``screenshot.svg`` there (the skill's references/ copy is made this way).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import store
from conftest import SCRIPTS
from tui import DecideApp

FIXTURE = Path(__file__).parent / "fixtures" / "e2e_decisions.json"


def cli(*args: str, stdin: str = "") -> str:
    """Run the CLI and return stdout (fails the test on a non-zero exit)."""
    r = subprocess.run(
        [sys.executable, str(SCRIPTS / "houston.py"), *args],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert r.returncode == 0, r.stderr
    return r.stdout


@pytest.mark.asyncio
async def test_end_to_end(tmp_path: Path) -> None:
    """The whole loop with real subprocess writes and the pilot-driven inbox."""
    data = json.loads(FIXTURE.read_text())
    ids: dict[str, str] = {}
    links: dict[str, list[dict[str, str]]] = {}
    for d in data["decisions"]:
        seed_id, seed_links = d.pop("seed_id"), d.pop("links_seed")
        if d["options"] is None:
            d.pop("options")
        ids[seed_id] = json.loads(cli("add", "--json", stdin=json.dumps(d)))["id"]
        links[seed_id] = seed_links
    for t in data["threads"]:
        cli("thread", "set", t["slug"], "--goal", t["goal"], "--state", t["state"], "--note", t["note"])
    for seed_id, lns in links.items():
        keep = [{"to": ids[x["to"]], "rel": x["rel"]} for x in lns if x["to"] in ids]
        if keep:
            cli("update", ids[seed_id], stdin=json.dumps({"links": keep}))
    assert len(cli("list").strip().splitlines()) == 6

    app = DecideApp(ask_fn=lambda p, c: "stub", reload_every=0)
    async with app.run_test(size=(150, 46)) as pilot:
        await pilot.pause()
        await pilot.press("g", "2")  # api-v2 thread
        await pilot.pause()
        c01 = app.by_id[ids["c01"]]
        app.query_one("#list").highlighted = app.rows.index(c01)
        await pilot.pause()
        await pilot.press("enter")  # stage Claude's star (ask Bob)
        await pilot.pause()
        c02 = app.by_id[ids["c02"]]
        app.query_one("#list").highlighted = app.rows.index(c02)
        await pilot.pause()
        await pilot.press("n", *"only the staging run", "enter")
        await pilot.pause()
        c05 = app.by_id[ids["c05"]]
        app.query_one("#list").highlighted = app.rows.index(c05)
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()
        await pilot.press("g", "3")  # mobile-port thread
        await pilot.pause()
        n02 = app.by_id[ids["n02"]]
        app.query_one("#list").highlighted = app.rows.index(n02)
        await pilot.pause()
        await pilot.press(str(n02.explain_row), "enter", *"what is a golden", "enter")
        await pilot.pause()
        n01 = app.by_id[ids["n01"]]
        app.query_one("#list").highlighted = app.rows.index(n01)
        await pilot.pause()
        await pilot.press("p", "3")
        await pilot.pause()
        assert app.staged_in(None) == 5
        await pilot.press("g", "2")
        await pilot.pause()
        app.query_one("#list").highlighted = app.rows.index(c02)
        await pilot.pause()
        shot_dir = os.environ.get("HOUSTON_SCREENSHOT") or str(tmp_path)
        app.save_screenshot(filename="screenshot.svg", path=shot_dir)
        await pilot.press("s")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert app.staged_in(None) == 0
        await pilot.press("q")

    out = cli("answers", "--session", "e2e-api-v2", "--mark-delivered")
    assert "3 new answers" in out and "picked 2 ask Bob" in out and "only the staging run" in out
    assert "you (Claude) pick" in out and "marked 3 delivered" in out
    out = cli("answers", "--thread", "mobile-port", "--json")
    rows = json.loads(out)
    assert {r["answer"]["kind"] for r in rows} == {"explain", "park"}
    assert next(r for r in rows if r["answer"]["kind"] == "park")["answer"]["until_event"] == "PR #93 merges"
    assert "No new answers." in cli("answers", "--session", "e2e-api-v2")
    assert store.effective_state(store.load_item(ids["n02"])) == "explaining"
    assert (Path(shot_dir) / "screenshot.svg").exists()
    assert "OK" in cli("doctor")
