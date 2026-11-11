"""The inbox driven headless with Textual's pilot: the mock's flows plus persistence, live reload, ? and c."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import store
from conftest import option, write_transcript
from tui import DecideApp, QuitScreen, ReviewScreen

SIZE = (140, 40)


def seed(make: Any, tmp_path: Path) -> list[str]:
    """Seven decisions in one thread and two in another; the 7th links to the mobile one."""
    tr = write_transcript(
        tmp_path / "src.jsonl",
        [
            ("user", "ok push it and open the PR", "2026-11-06T10:00:00Z"),
            ("assistant", "PR #45 is open. Who should review it?", "2026-11-06T10:01:00Z"),
        ],
    )
    ids = []
    for i in range(7):
        ids.append(
            store.add(
                make(
                    title=f"API question {'abcdefg'[i]}: topic {i}",
                    question=f"Should we do step {i} of the rollout plan now?",
                    source={"session_id": "sess-a", "cwd": "/tmp", "transcript": str(tr)},
                )
            )[0]
        )
    mob = store.add(
        make(
            thread={"slug": "mobile-port", "name": "Mobile app port"},
            title="Ask Alice to review PR #93?",
            question="Do we ask Alice today to review PR #93?",
            options=[option("yes, ping Alice", star=True), option("wait")],
        )
    )[0]
    store.add(make(thread="mobile-port", title="Port the screens?", question="Port the 12 screens to mobile now?"))
    store.update(ids[6], {"links": [{"to": mob, "rel": "the port needs this first"}]})
    store.set_thread("api-v2", goal="Ship API v2", state="blocked on you", note="PR #45 waits")
    return [*ids, mob]


@pytest.mark.asyncio
async def test_mock_flows_on_the_store(make: Any, tmp_path: Path) -> None:
    """Stage, restage, note, explain, park, drop, undo, link jump, quit guard, submit batch, undo batch."""
    seed(make, tmp_path)
    app = DecideApp(ask_fn=lambda p, c: "stub", reload_every=0)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        p0 = app.pending_in(None)
        await pilot.press("l")
        assert app.tab_idx == 1
        await pilot.press("h")
        assert app.tab_idx == 0
        await pilot.press("g", "2")
        assert app.tabs[app.tab_idx] == "api-v2"
        assert "Ship API v2" in str(app.query_one("#thread_head").render())

        d1 = app.current()
        assert d1 is not None and d1.cursor == d1.pick
        await pilot.press("3")
        assert d1.cursor == 3 and d1.staged is None
        await pilot.press("K", "K")
        assert d1.cursor == 1
        await pilot.press("J")
        assert d1.cursor == 2

        await pilot.press("1", "enter")
        await pilot.pause()
        assert d1.staged == {"kind": "option", "option": 1, "label": "merge now", "note": ""}
        assert store.load_staged()["staged"][d1.id]["option"] == 1, "staging persists at once"
        assert store.load_item(d1.id)["state"] == "pending", "staging does not answer"
        assert app.pending_in(None) == p0
        assert app.current() is not d1

        d2 = app.current()
        await pilot.press("k")
        assert app.current() is d1 and d1.cursor == 1
        await pilot.press("2", "enter")
        await pilot.pause()
        assert d1.staged["option"] == 2 and app.staged_in(None) == 1

        assert app.current() is d2
        await pilot.press(str(d2.note_row), "enter")
        await pilot.pause()
        await pilot.press(*"ask Alice first", "escape")
        await pilot.pause()
        assert d2.note_text == "ask Alice first" and d2.staged is None
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert d2.staged == {"kind": "note", "note": "ask Alice first"}

        d3 = app.current()
        await pilot.press("n", *"only if cheap", "enter")
        await pilot.pause()
        assert d3.staged["kind"] == "option" and d3.staged["note"] == "only if cheap"

        d4 = app.current()
        await pilot.press(str(d4.explain_row), "enter", *"what is a golden", "enter")
        await pilot.pause()
        assert d4.staged == {"kind": "explain", "note": "what is a golden"}

        d5 = app.current()
        await pilot.press("p")
        await pilot.pause()
        await pilot.press("2")
        await pilot.pause()
        assert d5.staged["kind"] == "park" and d5.staged["until"] == "next week" and d5.staged["until_date"]
        d6 = app.current()
        await pilot.press("x")
        await pilot.pause()
        assert d6.staged == {"kind": "drop"}
        app.query_one("#list").highlighted = app.rows.index(d6)
        await pilot.pause()
        await pilot.press("u")
        assert d6.staged is None
        n_staged = app.staged_in(None)
        assert n_staged == 5 and len(store.load_staged()["staged"]) == 5

        src = next(d for d in app.rows if d.links)
        app.query_one("#list").highlighted = app.rows.index(src)
        await pilot.pause()
        await pilot.press("o", "1")
        await pilot.pause()
        assert app.current().id == src.links[0]["to"]
        assert "Next in" not in app.bridge, "the bridge only links decisions inside one thread"

        await pilot.press("q")
        await pilot.pause()
        assert isinstance(app.screen, QuitScreen)
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, QuitScreen)

        await pilot.press("s")
        await pilot.pause()
        assert isinstance(app.screen, ReviewScreen)
        await pilot.press("escape")
        await pilot.pause()
        assert app.staged_in(None) == n_staged
        await pilot.press("s")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        batches = [e for e in store.read_events() if e["event"] == "batch_submitted"]
        assert len(batches) == 1 and len(batches[0]["ids"]) == n_staged
        assert set(batches[0]["kinds"]) == {"option", "note", "explain", "park"}
        assert app.staged_in(None) == 0 and store.load_staged()["staged"] == {}
        states = {i: store.load_item(i)["state"] for i in (d1.id, d2.id, d4.id, d5.id)}
        assert states == {d1.id: "answered", d2.id: "answered", d4.id: "explaining", d5.id: "parked"}
        assert d4.status == "explaining" and app.pending_in(None) == p0 - (n_staged - 1), "explain stays pending"
        assert "Submitted" in app.last_msg
        assert {r["id"] for r in store.pending_answers(session="sess-a")} == set(batches[0]["ids"])

        app.query_one("#list").highlighted = 0
        await pilot.pause()
        await pilot.press("u")
        await pilot.pause()
        assert app.staged_in(None) == n_staged and d1.status == "pending" and d4.status == "pending"
        assert store.load_item(d1.id)["state"] == "pending" and store.pending_answers(session="sess-a") == []
        await pilot.press("s")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        summary = app.summary()
        await pilot.press("q")
    assert f"{n_staged} submitted this session" in summary


@pytest.mark.asyncio
async def test_staged_picks_survive_restart(make: Any, tmp_path: Path) -> None:
    """Staged picks and typed drafts come back in a new app."""
    seed(make, tmp_path)
    app = DecideApp(reload_every=0)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        d = app.current()
        await pilot.press("1", "enter")
        await pilot.pause()
        e = app.current()
        await pilot.press(str(e.note_row), "enter", *"draft words", "escape")
        await pilot.pause()
        await pilot.press("q")
        await pilot.pause()
        await pilot.press("d")  # discard and quit
    assert store.load_staged()["staged"] == {}, "d discards and persists the discard"

    app = DecideApp(reload_every=0)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        d = app.current()
        await pilot.press("1", "enter")
        await pilot.pause()
        e = app.current()
        await pilot.press(str(e.note_row), "enter", *"draft words", "escape")
        await pilot.pause()
        did, eid = d.id, e.id
    app2 = DecideApp(reload_every=0)
    async with app2.run_test(size=SIZE) as pilot:
        await pilot.pause()
        assert app2.by_id[did].staged["option"] == 1
        assert app2.by_id[eid].note_text == "draft words"
        assert app2.staged_in(None) == 1


@pytest.mark.asyncio
async def test_live_reload_shows_new_decision(make: Any, tmp_path: Path) -> None:
    """A decision added by a session while the inbox is open appears without a key press; r reloads too."""
    seed(make, tmp_path)
    app = DecideApp(reload_every=0.1)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        n = len(app.decisions)
        did, _, _ = store.add(
            make(
                thread={"slug": "new-thread", "name": "Brand new"},
                title="Fresh one?",
                question="Is this brand new item visible?",
            )
        )
        await pilot.pause(0.4)
        assert did in app.by_id and len(app.decisions) == n + 1 and "new-thread" in app.tabs
        store.close(did, "moot")
        await pilot.press("r")
        await pilot.pause()
        assert app.by_id[did].status == "closed"


@pytest.mark.asyncio
async def test_ask_and_context(make: Any, tmp_path: Path) -> None:
    """C shows the transcript turns; ? runs the (stubbed) model off the UI thread and stores the Q&A."""
    seed(make, tmp_path)
    prompts: list[str] = []

    def fake(prompt: str, cwd: str | None) -> str:
        """Stand in for claude -p."""
        prompts.append(prompt)
        return "It means the picker is ready; only the review is missing."

    app = DecideApp(ask_fn=fake, reload_every=0)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await pilot.press("g", "2")
        d = app.current()
        await pilot.press("c")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.ctx_cache[d.id][0] == "transcript"
        assert "ok push it" in str(app.query_one("#ctx_body").render())
        await pilot.press("question_mark", *"plain words?", "enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert prompts and "plain words?" in prompts[0] and "ok push it" in prompts[0]
        assert d.history[-1] == ("plain words?", "It means the picker is ready; only the review is missing.")
        assert store.load_item(d.id)["qa"][-1]["source"] == "asked"


@pytest.mark.asyncio
async def test_ask_fails_soft(make: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing claude binary gives one clear line, no crash."""
    import ask

    seed(make, tmp_path)
    monkeypatch.setenv("HOUSTON_CLAUDE_BIN", str(tmp_path / "no-such-claude"))
    assert ask.ask_claude("hi").startswith("(no answer: could not start claude")
    app = DecideApp(reload_every=0)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        d = app.current()
        await pilot.press("question_mark", *"why?", "enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert d.history[-1][1].startswith("(no answer")


@pytest.mark.asyncio
async def test_empty_store() -> None:
    """With nothing in the store the inbox says so and keys do nothing harmful."""
    app = DecideApp(reload_every=0)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        assert "No pending decisions" in str(app.query_one("#detail_body").render())
        await pilot.press("enter", "x", "s", "u", "l", "c")
        await pilot.pause()
        await pilot.press("q")
