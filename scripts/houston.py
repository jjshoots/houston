# /// script
# requires-python = ">=3.10"
# dependencies = ["textual>=0.80"]
# ///
"""Decide: one inbox for every decision that waits on you, across all Claude Code sessions and worktrees.

Sessions write:  add, update, close, thread set, answers --mark-delivered
You read and answer:  tui (the default with no subcommand)
Every subcommand except ``tui`` is stdlib only, so ``python3 houston.py <cmd>`` works without uv.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import store  # noqa: E402

EXAMPLE = {
    "thread": {"slug": "login-refactor", "name": "Login refactor"},
    "kind": "go_ahead",
    "title": "Merge the login refactor (PR #12)?",
    "question": "Who reviews PR #12 (the new login flow) so it can merge?",
    "options": [
        {
            "label": "merge now",
            "then": "the new login flow lands on main today; the release is a separate step",
            "means": "You merge PR #12 yourself now. Nothing ships to users yet.",
            "next_steps": ["Merge the PR", "Tell the team it is merged"],
            "cost": "$0, 5 minutes",
            "risk": "No second reader before main changes.",
        },
        {
            "label": "ask Bob to review today",
            "then": "one fast review from someone who knows the auth code",
            "means": "Bob reviews today. He wrote the old session code.",
            "next_steps": ["Set Bob as reviewer", "Merge when he approves"],
            "cost": "$0, about 1 day",
            "risk": "A shallow review is possible if he is busy.",
            "star": True,
            "why_pick": "Bob knows the session code best and can read it today.",
        },
    ],
    "background": [
        "The login refactor moves sign-in to one shared session service.",
        "All tests pass and the staging check is green.",
        "PR #12 is open; nobody is set as reviewer.",
    ],
    "blocks": ["the release that removes the old login page"],
    "source": {"pointer": "PR #12"},
    "links": [],
    "urgency": 2,
}


def read_input(path: str | None) -> Any:
    """Read one JSON object from a file path or from stdin."""
    return read_input_text(Path(path).read_text(encoding="utf-8") if path and path != "-" else sys.stdin.read())


def read_input_text(text: str) -> Any:
    """Parse one JSON object from text, with an actionable error."""
    if not text.strip():
        raise store.ValidationError(["no input: pipe one JSON object on stdin or pass --file (see --example)"])
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise store.ValidationError([f"input is not valid JSON: {e}"]) from e


def short(item: dict[str, Any]) -> str:
    """Return one list line for a decision."""
    st = store.effective_state(item)
    asked = f" x{item.get('times_asked', 1)}" if item.get("times_asked", 1) > 1 else ""
    return f"{item['id']}  {st:<10} {item['thread']:<18} [{item['kind']}]{asked}  {item['title']}"


def show_text(item: dict[str, Any]) -> str:
    """Return a full plain-text view of one decision."""
    lines = [
        f"{item['id']} · {item['thread']} · {item['kind']} · {store.effective_state(item)} · "
        f"asked x{item.get('times_asked', 1)} · {store.age_days(item)}d old",
        "",
        item["title"],
        item["question"],
        "",
    ]
    lines += [f"  · {b}" for b in item.get("background", [])]
    lines.append("")
    for i, o in enumerate(item["options"], 1):
        star = "★" if i == item.get("pick") else " "
        lines.append(f" {star}{i} {o['label']} — {o['then']}")
        lines.append(f"     means: {o['means']}")
        lines.append(f"     next:  {' / '.join(o['next_steps'])}")
        lines.append(f"     cost:  {o['cost']}    risk: {o['risk']}")
        if o.get("why_pick"):
            lines.append(f"     why:   {o['why_pick']}")
    if item.get("blocks"):
        lines.append("Waits on this: " + "; ".join(item["blocks"]))
    for rec in item.get("answers", []):
        dl = f"delivered {rec['delivered_at']} to {rec['delivered_to']}" if rec.get("delivered_at") else "undelivered"
        lines.append(f"Answer {rec['ts']}: {store.answer_text(rec)} ({dl})")
    if item.get("closed"):
        lines.append(f"Closed by Claude: {item['closed']['reason']}")
    src = item.get("source", {})
    lines.append(
        f"From: session {src.get('session_id', '?')} · {src.get('cwd', '')} @ {src.get('branch', '')} · "
        f"{src.get('pointer', '')}"
    )
    return "\n".join(lines)


def cmd_add(a: argparse.Namespace) -> int:
    """Record a decision (or re-ask an existing one)."""
    if a.example:
        print(json.dumps(EXAMPLE, indent=1, ensure_ascii=False))
        return 0
    raw = None
    if not a.same_as or a.file is not None:
        raw = read_input(a.file)
    elif not sys.stdin.isatty():
        text = sys.stdin.read()
        raw = read_input_text(text) if text.strip() else None
    did, created, warns = store.add(raw, same_as=a.same_as)
    for w in warns:
        print(f"warning: {w}", file=sys.stderr)
    item = store.load_item(did)
    if a.json:
        print(json.dumps({"id": did, "created": created, "times_asked": item["times_asked"]}))
    elif created:
        print(f"added {did}")
    else:
        print(
            f"already in the inbox as {did} (asked x{item['times_asked']}); not added again. "
            f"Use `update {did}` to change its text."
        )
    return 0


def cmd_threads(a: argparse.Namespace) -> int:
    """List threads with their pending counts."""
    items = store.all_items()
    rows = []
    for slug, t in sorted(store.threads().items()):
        n = sum(1 for i in items if i["thread"] == slug and store.effective_state(i) in store.OPEN_STATES)
        rows.append({**t, "pending": n})
    if a.json:
        print(json.dumps(rows, indent=1, ensure_ascii=False))
        return 0
    if not rows:
        print('no threads yet; `add` creates one from {"thread": {"slug", "name"}}')
    for r in rows:
        print(f"{r['slug']:<20} {r['pending']:>2} pending  {r['name']}")
        if r.get("goal") or r.get("state"):
            print(f"{'':<20} goal: {r.get('goal', '')} · now: {r.get('state', '')} {r.get('state_note', '')}")
    return 0


def cmd_thread_set(a: argparse.Namespace) -> int:
    """Create or change a thread's name, goal or state."""
    t = store.set_thread(a.slug, name=a.name, goal=a.goal, state=a.state, note=a.note)
    print(f"thread {t['slug']}: {t['name']} · goal: {t['goal']} · now: {t['state']} {t['state_note']}")
    return 0


def cmd_list(a: argparse.Namespace) -> int:
    """List decisions with filters."""
    items = store.all_items()
    if a.thread:
        items = [i for i in items if i["thread"] == a.thread]
    if a.session:
        items = [i for i in items if a.session in i.get("sessions", [])]
    if a.state != "all":
        want = store.OPEN_STATES if a.state == "open" else (a.state,)
        items = [i for i in items if store.effective_state(i) in want]
    if a.json:
        print(json.dumps(items, indent=1, ensure_ascii=False))
        return 0
    for i in items:
        print(short(i))
    if not items:
        print("No decisions match.")
    return 0


def cmd_show(a: argparse.Namespace) -> int:
    """Show one decision."""
    item = store.load_item(a.id)
    print(json.dumps(item, indent=1, ensure_ascii=False) if a.json else show_text(item))
    return 0


def cmd_update(a: argparse.Namespace) -> int:
    """Rewrite a decision in place (answers an explain request)."""
    item = store.update(a.id, read_input(a.file))
    print(f"updated {item['id']} (state {item['state']})")
    return 0


def cmd_close(a: argparse.Namespace) -> int:
    """Close a decision as moot or superseded."""
    item = store.close(a.id, a.reason)
    print(f"closed {item['id']}: {a.reason}")
    return 0


def cmd_answers(a: argparse.Namespace) -> int:
    """Print answers not yet delivered; optionally record delivery."""
    if not (a.session or a.thread or a.all):
        raise store.ValidationError(
            ["answers: give --session <id> (yours: $CLAUDE_CODE_SESSION_ID), --thread " "<slug>, or --all"]
        )
    rows = store.pending_answers(session=a.session, thread=a.thread)
    if a.json:
        print(json.dumps(rows, indent=1, ensure_ascii=False))
    elif not rows:
        print("No new answers.")
    else:
        print(f"{len(rows)} new answer{'s' * (len(rows) != 1)} from {store.user_name()}:")
        for r in rows:
            print(f"- {r['id']} [{r['thread']}] {r['title']}\n    → {r['text']}")
            if r.get("option"):
                print(f"      means: {r['option']['means']}")
                print(f"      next:  {' / '.join(r['option']['next_steps'])}")
        print(
            "Act on each. For 'explain this better', rewrite with `update <id>`; for 'you pick', decide and "
            "act on your star; for a park, do nothing until it returns."
        )
    if a.mark_delivered and rows:
        to = a.session or (f"thread:{a.thread}" if a.thread else "all")
        n = store.mark_delivered(rows, to)
        if not a.json:
            print(f"(marked {n} delivered to {to})")
    return 0


def cmd_doctor(a: argparse.Namespace) -> int:
    """Report store health."""
    rep = store.doctor()
    if a.json:
        print(json.dumps(rep, indent=1))
    else:
        print(f"store {rep['home']}: {rep['items']} decisions in {rep['threads']} threads, {rep['events']} events")
        print("states: " + (", ".join(f"{k} {v}" for k, v in sorted(rep["states"].items())) or "none"))
        print(f"staged in inbox: {rep['staged']} · undelivered answers: {rep['undelivered_answers']}")
        if rep["orphan_temp_files"]:
            print(f"orphan temp files: {', '.join(rep['orphan_temp_files'])}")
        for e in rep["errors"]:
            print(f"ERROR {e}")
        print("OK" if not rep["errors"] else f"{len(rep['errors'])} problems")
    return 1 if rep["errors"] else 0


def cmd_tui(a: argparse.Namespace) -> int:
    """Open the inbox."""
    import tui  # textual is only needed here

    result = tui.DecideApp().run()
    if result:
        print(result)
    return 0


def parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    p = argparse.ArgumentParser(prog="houston", description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("add", help="record a decision (JSON on stdin or --file)")
    s.add_argument("--file", "-f")
    s.add_argument("--same-as", help="re-ask this existing decision id (stdin JSON optional: replaces its text)")
    s.add_argument("--example", action="store_true", help="print a valid example input and exit")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_add)

    s = sub.add_parser("threads", help="list threads (reuse a slug before inventing one)")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_threads)

    s = sub.add_parser("thread", help="thread set <slug> [--name] [--goal] [--state] [--note]")
    ts = s.add_subparsers(dest="thread_cmd", required=True)
    s2 = ts.add_parser("set")
    s2.add_argument("slug")
    s2.add_argument("--name")
    s2.add_argument("--goal")
    s2.add_argument("--state", help="e.g. 'blocked on you', 'running', 'done'")
    s2.add_argument("--note", help="one line after the state, e.g. 'PR #445 waits on a review'")
    s2.set_defaults(fn=cmd_thread_set)

    s = sub.add_parser("list", help="list decisions")
    s.add_argument("--thread")
    s.add_argument("--session")
    s.add_argument("--state", default="open", choices=["open", "all", *store.STATES])
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_list)

    s = sub.add_parser("show", help="show one decision")
    s.add_argument("id")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_show)

    s = sub.add_parser("update", help="rewrite a decision (JSON with the fields to replace)")
    s.add_argument("id")
    s.add_argument("--file", "-f")
    s.set_defaults(fn=cmd_update)

    s = sub.add_parser("close", help="close a decision as moot or superseded")
    s.add_argument("id")
    s.add_argument("--reason", required=True)
    s.set_defaults(fn=cmd_close)

    s = sub.add_parser("answers", help="answers not yet delivered to a session or thread")
    s.add_argument("--session")
    s.add_argument("--thread")
    s.add_argument("--all", action="store_true")
    s.add_argument("--mark-delivered", action="store_true")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_answers)

    s = sub.add_parser("doctor", help="store health")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_doctor)

    s = sub.add_parser("tui", help="open the inbox (default)")
    s.set_defaults(fn=cmd_tui)
    return p


def main(argv: list[str] | None = None) -> int:
    """Run one subcommand; validation problems print one line each and exit 2."""
    a = parser().parse_args(argv)
    if not getattr(a, "fn", None):
        a.fn = cmd_tui
    try:
        return a.fn(a)
    except store.ValidationError as e:
        print(
            f"houston {a.cmd or 'tui'}: {len(e.errors)} problem{'s' * (len(e.errors) != 1)}; fix and run again:",
            file=sys.stderr,
        )
        for err in e.errors:
            print(f"  - {err}", file=sys.stderr)
        return 2
    except store.StoreError as e:
        print(f"houston {a.cmd}: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
