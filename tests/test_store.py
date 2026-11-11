"""Store: atomic writes, dedupe, validation, state changes, answers and transcript reading."""

from __future__ import annotations

import datetime as dt
import json
import os
import threading
from pathlib import Path
from typing import Any

import pytest
import store
from conftest import option, write_transcript


def test_atomic_write_leaves_no_temp_and_survives_failure(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed write keeps the old file and leaves no temp file behind."""
    target = home / "items" / "x.json"
    store.atomic_write_json(target, {"a": 1})
    assert json.loads(target.read_text()) == {"a": 1}

    def boom(*a: Any, **k: Any) -> None:
        """Fail the rename."""
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        store.atomic_write_json(target, {"a": 2})
    assert json.loads(target.read_text()) == {"a": 1}
    assert not list((home / "items").glob(".*.tmp"))


def test_add_creates_item_thread_and_event(make: Any) -> None:
    """Add writes one item file, a thread and an 'asked' event."""
    did, created, _ = store.add(make())
    assert created
    item = store.load_item(did)
    assert item["state"] == "pending" and item["pick"] == 2 and item["times_asked"] == 1
    assert item["sessions"] == ["sess-a"]
    assert store.threads()["api-v2"]["name"] == "API v2 rollout"
    assert [e["event"] for e in store.read_events()] == ["asked"]


def test_dedupe_bumps_times_asked(make: Any) -> None:
    """A near-identical decision in the same thread re-asks the open one."""
    did, _, _ = store.add(make())
    again, created, _ = store.add(make(title="Merge API v2 (PR #45)", source={"session_id": "sess-b"}))
    assert again == did and not created
    item = store.load_item(did)
    assert item["times_asked"] == 2 and item["sessions"] == ["sess-a", "sess-b"]
    assert len(list(store.items_dir().glob("*.json"))) == 1
    other, created, _ = store.add(make(title="Spend $40 on a load test?", question="Approve $40 for a load test?"))
    assert created and other != did
    elsewhere, created, _ = store.add(make(thread={"slug": "mobile-port", "name": "mobile port"}))
    assert created and elsewhere != did, "dedupe is per thread"


def test_dedupe_skips_closed_and_same_as_reopens(make: Any) -> None:
    """A closed decision is not a dedupe target; --same-as re-asks and reopens it."""
    did, _, _ = store.add(make())
    store.close(did, "superseded by the v32 plan")
    new, created, _ = store.add(make())
    assert created and new != did
    back, created, _ = store.add(None, same_as=did)
    assert back == did and not created
    item = store.load_item(did)
    assert item["state"] == "pending" and item["times_asked"] == 2


def test_concurrent_adds_never_clobber(make: Any) -> None:
    """Parallel writers each get their own file; same-thread duplicates collapse to one."""
    errs: list[Exception] = []

    def worker(i: int) -> None:
        """Add one distinct and one duplicate decision."""
        try:
            store.add(
                make(
                    title=f"Distinct question number {i} about topic {i * 7}",
                    question=f"Do we run job {i} on shard {i * 13} tonight?",
                )
            )
            store.add(make())
        except Exception as e:  # noqa: BLE001
            errs.append(e)

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errs
    items = store.all_items()
    assert len(items) == 9
    dup = [i for i in items if i["title"] == "Merge API v2 (PR #45)?"]
    assert len(dup) == 1 and dup[0]["times_asked"] == 8


@pytest.mark.parametrize(
    ("over", "needle"),
    [
        ({"options": [option(f"o{i}", star=i == 0) for i in range(6)]}, "limit 5"),
        ({"options": [option("a"), option("b")]}, "star exactly one"),
        ({"options": [option("a", star=True), option("b", star=True)]}, "star exactly one"),
        ({"options": [{**option("a", star=True), "risk": ""}, option("b")]}, "options[1].risk"),
        ({"options": [{**option("a", star=True), "why_pick": ""}, option("b")]}, "why_pick"),
        ({"options": [option("a", star=True), {**option("b"), "next_steps": []}]}, "next_steps"),
        ({"title": "x" * 81}, "limit 80"),
        ({"question": "Line one\nline two?"}, "one plain sentence"),
        ({"kind": "whatever"}, "kind"),
        ({"thread": {"slug": "Bad Slug"}}, "thread"),
        ({"thread": "new-thread"}, "is new"),
        ({"background": []}, "background"),
        ({"source": {"cwd": "/tmp"}}, "session_id"),
        ({"urgency": 9}, "urgency"),
        ({"extra": 1}, "unknown fields"),
    ],
)
def test_validation_errors(make: Any, over: dict[str, Any], needle: str) -> None:
    """Each broken field gives an actionable error naming it."""
    raw = make(**over)
    if "blocks" in over:
        raw.pop("blocks")
    with pytest.raises(store.ValidationError) as e:
        store.add(raw)
    assert any(needle in err for err in e.value.errors), e.value.errors
    assert not store.all_items()


def test_missing_blocks_and_many_errors_at_once(make: Any) -> None:
    """All problems come back in one go."""
    raw = make(title="", question="")
    raw.pop("blocks")
    with pytest.raises(store.ValidationError) as e:
        store.add(raw)
    joined = " | ".join(e.value.errors)
    assert "title" in joined and "question" in joined and "blocks" in joined


def test_default_options_for_claude_decided(make: Any) -> None:
    """Claude_decided without options gets keep / reverse / explain, star on keep."""
    did, _, _ = store.add(make(kind="claude_decided", options=None, title="Claude raised the retry cap to 5"))
    item = store.load_item(did)
    assert [o["label"] for o in item["options"]] == ["keep", "reverse", "explain"] and item["pick"] == 1


def test_source_autofill_from_env(make: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Session_id comes from $CLAUDE_CODE_SESSION_ID when the writer leaves it out."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "env-sess")
    did, _, _ = store.add(make(source={"pointer": "PR #1"}))
    src = store.load_item(did)["source"]
    assert src["session_id"] == "env-sess" and src["cwd"] == os.getcwd() and src["pointer"] == "PR #1"


def test_submit_answers_deliver_and_undo(make: Any) -> None:
    """Submit writes one batch; answers are per session; delivery is recorded; undo restores."""
    a, _, _ = store.add(make())
    b, _, _ = store.add(
        make(title="Spend $40 on a load test?", question="Approve $40 for the load test?", source={"session_id": "sess-b"})
    )
    batch, snaps = store.submit_batch(
        {
            a: {"kind": "option", "option": 1, "label": "merge now", "note": "today"},
            b: {"kind": "explain", "note": "what is a load test"},
        }
    )
    assert store.load_item(a)["state"] == "answered" and store.load_item(b)["state"] == "explaining"
    rows = store.pending_answers(session="sess-a")
    assert [r["id"] for r in rows] == [a] and "picked 1 merge now" in rows[0]["text"]
    assert rows[0]["option"]["next_steps"]
    assert store.mark_delivered(rows, "sess-a") == 1
    assert store.pending_answers(session="sess-a") == []
    assert store.load_item(a)["answers"][0]["delivered_to"] == "sess-a"
    store.undo_batch(batch, snaps)
    assert store.load_item(a)["state"] == "pending" and store.load_item(b)["state"] == "pending"
    assert store.load_item(b)["answers"] == []
    retract = store.pending_answers(session="sess-a")
    assert [r["answer"]["kind"] for r in retract] == ["retract"], "delivered answers are retracted, not erased"
    assert {"batch_submitted", "delivered", "batch_undone"} <= {e["event"] for e in store.read_events()}


def test_update_clears_explain(make: Any) -> None:
    """Update rewrites the text and sends an explain request back to pending."""
    did, _, _ = store.add(make())
    store.submit_batch({did: {"kind": "explain", "note": "what is API v2"}})
    store.update(
        did,
        {
            "background": [
                "API v2 = the second version of the public API.",
                "It is ready.",
                "Only the review is missing.",
            ]
        },
    )
    item = store.load_item(did)
    assert item["state"] == "pending" and item["explain"] is None
    assert item["background"][0].startswith("API v2 =") and item["answers"], "answer history is kept"
    with pytest.raises(store.ValidationError):
        store.update(did, {"options": [option("only")]})


def test_park_returns_when_date_passes(make: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """A park with a date shows as pending once the date arrives; event parks wait for unpark."""
    a, _, _ = store.add(make())
    b, _, _ = store.add(make(title="Spend $40 on a load test?", question="Approve $40 for the load test?"))
    tomorrow = (dt.date.today() + dt.timedelta(days=1)).isoformat()
    store.submit_batch(
        {a: {"kind": "park", "until_date": tomorrow}, b: {"kind": "park", "until_event": "PR #45 merges"}}
    )
    assert store.effective_state(store.load_item(a)) == "parked"
    monkeypatch.setattr(store, "today", lambda: dt.date.today() + dt.timedelta(days=2))
    assert store.effective_state(store.load_item(a)) == "pending"
    assert store.effective_state(store.load_item(b)) == "parked"
    store.unpark(b)
    assert store.effective_state(store.load_item(b)) == "pending"


def test_close_keeps_file(make: Any) -> None:
    """Close marks the decision closed with the reason; the file stays."""
    did, _, _ = store.add(make())
    store.close(did, "moot: PR merged by Alice")
    item = store.load_item(did)
    assert item["state"] == "closed" and item["closed"]["reason"] == "moot: PR merged by Alice"


def test_transcript_context_streams_and_falls_back(make: Any, tmp_path: Path) -> None:
    """Context comes from the transcript around the ask, then the excerpt, then 'none'."""
    tr = write_transcript(
        tmp_path / "s.jsonl",
        [
            ("user", "ok push it and open the PR", "2026-11-06T10:00:00Z"),
            ("assistant", "PR #45 is open. Who should review it?", "2026-11-06T10:01:00Z"),
            ("tool", "git status", "2026-11-06T10:01:30Z"),
            ("user", "later", "2026-11-06T10:02:00Z"),
            ("assistant", "far future turn", "2030-01-01T00:00:00Z"),
        ],
    )
    did, _, _ = store.add(
        make(source={"session_id": "s", "cwd": "/tmp", "transcript": str(tr)}, context=[["USER", "stored excerpt"]])
    )
    item = store.load_item(did)
    item["asked_at"] = ["2026-11-06T10:02:30+00:00"]
    where, turns = store.context_for(item)
    assert where == "transcript"
    assert [t[0] for t in turns][:4] == ["USER", "ASSISTANT", "USER", "ASSISTANT"]
    assert all("git status" not in t[1] for t in turns)
    tr.unlink()
    assert store.context_for(item) == ("excerpt", [["USER", "stored excerpt"]])
    item["context"] = []
    assert store.context_for(item) == ("none", [])


def test_doctor_reports_problems(make: Any, home: Path) -> None:
    """Doctor counts states and flags broken files and dangling links."""
    did, _, _ = store.add(make(links=[{"to": "nope", "rel": "same PR"}]))
    (home / "items" / "broken.json").write_text("{not json")
    (home / "items" / ".x.json.123.tmp").write_text("")
    rep = store.doctor()
    assert rep["items"] == 1 and rep["states"] == {"pending": 1}
    joined = " ".join(rep["errors"])
    assert "broken.json" in joined and "nope" in joined
    assert rep["orphan_temp_files"]
