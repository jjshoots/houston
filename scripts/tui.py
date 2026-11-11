"""The decision inbox (Textual), ported from the approved mock onto the store.

Flow: move the option cursor, ``enter`` STAGES a choice, ``s`` reviews and submits all staged at once.
Staged picks live in the store (``staged.json``), so they survive a restart.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import ask
import store
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.css.query import NoMatches
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

ACCENT = "#ffb000"  # selection + Claude's star
STAGED = "#5fc8e8"  # staged-but-not-submitted
EXPLAIN = "#c792ea"  # asked Claude to explain
BG = "#0f1115"
DIM = "#8a929c"
SOFT = "#c8ccd2"
GLYPH = {
    "pending": "●",
    "answered": "✓",
    "parked": "⏸",
    "dropped": "✗",
    "handed": "↪",
    "explaining": "?",
    "closed": "⊘",
}
STAGED_GLYPH = "◆"
OPEN = store.OPEN_STATES  # both still count as pending
KIND_SHORT = {"tell_someone": "tell", "claude_decided": "Claude did", "go_ahead": "go-ahead"}
PARK_CHOICES = ["tomorrow", "next week", "after PR merges"]
MAX_SLOTS = 9
RELOAD_S = 3.0
SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


@dataclass
class Decision:
    """One decision as the inbox shows it (built from a store item; edits go back through the store)."""

    id: str
    thread: str
    kind: str
    title: str
    question: str
    options: list[dict[str, Any]]
    pick: int
    waits_on: list[str]
    src: dict[str, Any]
    context: list[list[str]]
    age_days: int = 0
    asked: int = 1
    urgency: int = 1
    status: str = "pending"
    answer: str = ""
    answered_option: int = 0
    until: str = ""
    history: list[tuple[str, str]] = field(default_factory=list)
    story: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    links: list[dict[str, Any]] = field(default_factory=list)
    cursor: int = 1
    staged: dict[str, Any] | None = None  # {"kind": option|note|explain|park|drop|you_pick, ...}
    note_text: str = ""  # "Write my own answer" row
    attach_note: str = ""  # note added to an option pick
    unclear: str = ""  # "Explain this better" row
    asking: str = ""  # question in flight to the headless model
    item: dict[str, Any] = field(default_factory=dict)

    @property
    def note_row(self) -> int:
        """Row number of "Write my own answer"."""
        return len(self.options) + 1

    @property
    def explain_row(self) -> int:
        """Row number of "Explain this better"."""
        return len(self.options) + 2

    @property
    def n_rows(self) -> int:
        """Rows in the option list (options + the two fixed rows)."""
        return len(self.options) + 2


def fields_from_item(item: dict[str, Any]) -> dict[str, Any]:
    """Map a store item onto Decision fields (everything that comes from disk)."""
    status = store.effective_state(item)
    answers = item.get("answers") or []
    last = answers[-1] if answers else None
    answer, answered_option, until = "", 0, ""
    if status == "closed":
        answer = f"closed by Claude: {(item.get('closed') or {}).get('reason', '')}"
    elif status == "explaining":
        answer = (item.get("explain") or {}).get("unclear", "")
    elif last and status in ("answered", "handed", "dropped"):
        answer = store.answer_text(last)
        answered_option = (last.get("option") or 0) if last["kind"] == "option" else 0
    if status == "parked":
        park = item.get("park") or {}
        until = park.get("until_event") or park.get("until_date") or ""
    return {
        "thread": item["thread"],
        "kind": item["kind"],
        "title": item["title"],
        "question": item["question"],
        "options": item["options"],
        "pick": item.get("pick", 1),
        "waits_on": item.get("blocks", []),
        "src": item.get("source", {}),
        "context": item.get("context", []),
        "age_days": store.age_days(item),
        "asked": item.get("times_asked", 1),
        "urgency": item.get("urgency", 1),
        "status": status,
        "answer": answer,
        "answered_option": answered_option,
        "until": until,
        "history": [(q["q"], q["a"]) for q in item.get("qa", [])],
        "story": item.get("background", []),
        "changed": item.get("changed_since", []),
        "links": item.get("links", []),
        "item": item,
    }


def park_target(d: Decision, choice: str) -> dict[str, Any]:
    """Turn a park choice into an absolute date, or a PR event for "after PR merges"."""
    if choice == "tomorrow":
        return {"until_date": (store.today() + dt.timedelta(days=1)).isoformat()}
    if choice == "next week":
        return {"until_date": (store.today() + dt.timedelta(days=7)).isoformat()}
    hay = f"{d.src.get('pointer', '')} {d.title} {d.question}"
    m = re.search(r"(?:PR\s*)?#\d+", hay)
    ref = m.group(0) if m else "the PR"
    if not ref.startswith("PR"):
        ref = f"PR {ref}"
    return {"until_event": f"{ref} merges"}


def age_str(days: int) -> str:
    """Return 'today' or 'Nd'."""
    return "today" if days == 0 else f"{days}d"


def stage_tag(st: dict[str, Any]) -> str:
    """Short text of a staged choice, for the list row."""
    k = st["kind"]
    if k == "option":
        return f"→ {st['option']} {st['label']}" + (" + note" if st.get("note") else "")
    if k == "park":
        return f"→ park: {st['until']}"
    if k == "you_pick":
        return "→ you pick"
    return f"→ {k}"


def stage_line(st: dict[str, Any]) -> str:
    """Longer text of a staged choice, for the review screen and detail banner."""
    k = st["kind"]
    if k == "option":
        s = f"{st['option']} {st['label']}"
    elif k == "note":
        s = "own answer"
    elif k == "explain":
        s = "explain this better"
    elif k == "park":
        s = f"park until {st['until']}"
    elif k == "drop":
        s = "drop (not needed)"
    else:
        s = f"you pick (★ {st['label']})"
    if st.get("note"):
        s += f" · “{st['note']}”"
    return s


class Pane(VerticalScroll):
    """Scroll pane whose left/right switch tabs instead of scrolling sideways."""

    BINDINGS = [
        Binding("left", "app.tab(-1)", show=False),
        Binding("right", "app.tab(1)", show=False),
    ]


class DetailPane(Pane):
    """Detail pane: up/down step the option cursor; page keys scroll."""

    BINDINGS = [
        Binding("down", "app.opt(1)", show=False),
        Binding("up", "app.opt(-1)", show=False),
    ]


class DecisionList(OptionList):
    """The left list; enter stages the option under the cursor."""

    BINDINGS = [Binding("enter", "app.accept", show=False)]


class ParkScreen(ModalScreen[str | None]):
    """Ask how long to park a decision."""

    BINDINGS = [
        Binding("1", "choose(0)"),
        Binding("2", "choose(1)"),
        Binding("3", "choose(2)"),
        Binding("escape", "dismiss_none"),
    ]

    def compose(self) -> ComposeResult:
        """Lay out the park choices."""
        with Vertical(id="modal_box"):
            yield Static(Text("Park until when?", style="bold"))
            yield OptionList(*[f"{i + 1}  {c}" for i, c in enumerate(PARK_CHOICES)], id="park_list")
            yield Static(Text("1-3 or enter · esc cancel", style=DIM))

    def on_mount(self) -> None:
        """Focus the list."""
        self.query_one(OptionList).focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        """Enter picks the highlighted choice."""
        self.dismiss(PARK_CHOICES[event.option_index])

    def action_choose(self, i: int) -> None:
        """A digit picks a choice."""
        self.dismiss(PARK_CHOICES[i])

    def action_dismiss_none(self) -> None:
        """Esc cancels."""
        self.dismiss(None)


class ReviewList(OptionList):
    """The review list; enter submits all."""

    BINDINGS = [Binding("enter", "screen.submit", show=False)]


class ReviewScreen(ModalScreen[bool]):
    """Every staged decision, grouped by thread.

    enter submits all.
    """

    BINDINGS = [
        Binding("j", "move(1)", show=False),
        Binding("k", "move(-1)", show=False),
        Binding("x", "unstage", show=False),
        Binding("escape", "back", show=False),
    ]

    def __init__(self, app_ref: DecideApp) -> None:
        """Keep a reference to the inbox."""
        super().__init__()
        self.inbox = app_ref

    def compose(self) -> ComposeResult:
        """Lay out the review box."""
        with Vertical(id="review_box"):
            yield Static(id="review_title")
            yield ReviewList(id="review_list")
            yield Static(
                Text.assemble(
                    ("enter", "bold"),
                    (" submit all   ", DIM),
                    ("esc", "bold"),
                    (" back to edit   ", DIM),
                    ("j/k", "bold"),
                    (" move   ", DIM),
                    ("x", "bold"),
                    (" unstage line", DIM),
                )
            )

    def on_mount(self) -> None:
        """Fill and focus the list."""
        self.rebuild()
        self.query_one(ReviewList).focus()

    def rebuild(self) -> None:
        """Redraw the staged lines grouped by thread."""
        staged = self.inbox.staged_list()
        ol = self.query_one(ReviewList)
        keep = ol.highlighted
        ol.clear_options()
        last_thread = None
        for d in staged:
            if d.thread != last_thread:
                ol.add_option(Option(Text(self.inbox.tname(d.thread).upper(), style=f"bold {DIM}"), disabled=True))
                last_thread = d.thread
            line = Text()
            line.append(f" {STAGED_GLYPH} ", style=f"bold {STAGED}")
            line.append(d.title, style="bold")
            line.append("  ·  ")
            line.append(stage_line(d.staged or {}), style=STAGED)
            ol.add_option(Option(line, id=d.id))
        n = len(staged)
        self.query_one("#review_title", Static).update(
            Text.assemble(("Review before submit", "bold"), (f"   {n} staged decision{'s' * (n != 1)}", STAGED))
        )
        if ol.option_count:
            ids = [i for i in range(ol.option_count) if not ol.get_option_at_index(i).disabled]
            if ids:
                ol.highlighted = keep if keep in ids else ids[0]

    def action_move(self, step: int) -> None:
        """J/k move the cursor."""
        ol = self.query_one(ReviewList)
        ol.action_cursor_down() if step > 0 else ol.action_cursor_up()

    def action_unstage(self) -> None:
        """X unstages the highlighted line."""
        ol = self.query_one(ReviewList)
        if ol.highlighted is None:
            return
        opt = ol.get_option_at_index(ol.highlighted)
        if opt.id:
            self.inbox.by_id[opt.id].staged = None
            self.inbox.persist_staged()
            self.rebuild()
            if not self.inbox.staged_list():
                self.dismiss(False)

    def action_submit(self) -> None:
        """Enter submits all."""
        self.dismiss(True)

    def action_back(self) -> None:
        """Esc goes back to editing."""
        self.dismiss(False)


class QuitScreen(ModalScreen[str]):
    """Guard against quitting with staged picks."""

    BINDINGS = [
        Binding("s", "choose('submit')", show=False),
        Binding("d", "choose('discard')", show=False),
        Binding("escape", "choose('cancel')", show=False),
    ]

    def __init__(self, n: int) -> None:
        """Keep the staged count."""
        super().__init__()
        self.n = n

    def compose(self) -> ComposeResult:
        """Lay out the warning."""
        with Vertical(id="modal_box", classes="wide"):
            yield Static(Text(f"{self.n} staged decision{'s' * (self.n != 1)} not submitted", style=f"bold {STAGED}"))
            yield Static(
                Text.assemble(
                    ("\ns", "bold"),
                    (" submit   ", DIM),
                    ("d", "bold"),
                    (" discard and quit   ", DIM),
                    ("esc", "bold"),
                    (" cancel", DIM),
                )
            )

    def action_choose(self, what: str) -> None:
        """Return the choice."""
        self.dismiss(what)


CSS = f"""
Screen {{ background: {BG}; color: #e6e6e6; }}
#top {{ height: 1; background: #181b21; }}
#title {{ width: 1fr; padding: 0 1; }}
#counts {{ width: auto; padding: 0 1; }}
#tabs {{ height: 1; padding: 0 1; background: {BG}; }}
#main {{ height: 1fr; }}
#left {{ width: 40%; border: round #2f353e; background: {BG}; }}
#left:focus-within {{ border: round #9aa3ad; }}
#right {{ width: 60%; }}
#detail {{ height: 1fr; border: round #2f353e; padding: 0 1; background: {BG}; }}
#detail:focus-within {{ border: round #9aa3ad; }}
#ctx {{ height: 45%; border: round #2f353e; display: none; background: #12151a; }}
#ctx.shown {{ display: block; }}
#ctx:focus-within {{ border: round #9aa3ad; }}
#ctx_scroll {{ height: 1fr; padding: 0 1; background: #12151a; }}
#ask, #filter {{ display: none; border: none; height: 1; padding: 0 1; background: #1d2129; }}
#ask.shown, #filter.shown {{ display: block; }}
#bridge {{ display: none; background: #2a2414; color: {ACCENT}; padding: 0 1; margin-bottom: 1; }}
#bridge.shown {{ display: block; }}
#thread_head {{ display: none; padding: 0 1; background: #161a20; color: {SOFT}; }}
#thread_head.shown {{ display: block; }}
#question {{ margin: 1 0; }}
.slot {{ height: auto; padding: 0 0 0 1; }}
.slot.hide {{ display: none; }}
.slot.cur {{ border-left: thick {ACCENT}; background: #1a1d23; padding: 0 1 0 0; margin: 0 0 1 0; }}
.slot.staged {{ border-left: thick {STAGED}; padding: 0 1 0 0; }}
.slot.cur.staged {{ border-left: thick {STAGED}; }}
.slot Static {{ height: auto; }}
.slot Input {{ display: none; height: 3; margin: 1 0 0 2; border: tall #3a414b; background: #12151a; }}
.slot Input:focus {{ border: tall {ACCENT}; }}
.slot.cur Input {{ display: block; }}
#footer {{ height: auto; background: #181b21; padding: 0 1; }}
DecisionList {{ height: 1fr; border: none; padding: 0; background: {BG}; }}
DecisionList > .option-list--option-highlighted {{ background: #3a3220; color: {ACCENT}; text-style: bold; }}
DecisionList:focus > .option-list--option-highlighted {{ background: #4a3a0c; color: {ACCENT}; text-style: bold; }}
DecisionList > .option-list--option-hover {{ background: #1d2129; }}
ModalScreen {{ align: center middle; background: {BG} 70%; }}
#modal_box {{ width: 46; height: auto; border: round {ACCENT}; background: #181b21; padding: 1 2; }}
#modal_box.wide {{ width: 70; }}
#park_list {{ height: auto; border: none; margin: 1 0; background: #181b21; }}
#park_list > .option-list--option-highlighted {{ background: {ACCENT}; color: {BG}; }}
#review_box {{ width: 100%; height: 90%; border: round {STAGED}; background: #181b21; padding: 1 2; }}
#review_list {{ height: 1fr; border: none; margin: 1 0; background: #181b21; }}
#review_list > .option-list--option-highlighted {{ background: #1f3a44; color: #ffffff; text-style: bold; }}
"""

AskFn = Callable[[str, "str | None"], str]


class DecideApp(App[str]):
    """The inbox app."""

    CSS = CSS
    TITLE = "Decision inbox"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        Binding("h,left_square_bracket,left", "tab(-1)", show=False),
        Binding("l,right_square_bracket,right", "tab(1)", show=False),
        Binding("j", "move(1)", show=False),
        Binding("k", "move(-1)", show=False),
        Binding("J", "opt(1)", show=False),
        Binding("K", "opt(-1)", show=False),
        *[Binding(str(i), f"digit({i})", show=False) for i in range(10)],
        *[Binding(f"alt+{i}", f"jump({i})", show=False) for i in range(10)],
        Binding("g", "g", show=False),
        Binding("o", "o", show=False),
        Binding("enter", "accept", show=False),
        Binding("n", "note", show=False),
        Binding("p", "park", show=False),
        Binding("x", "drop", show=False),
        Binding("y", "handoff", show=False),
        Binding("u", "undo", show=False),
        Binding("s", "submit", show=False),
        Binding("ctrl+s", "submit", show=False, priority=True),
        Binding("c", "toggle_context", show=False),
        Binding("question_mark", "ask", show=False),
        Binding("slash", "filter", show=False),
        Binding("a", "toggle_answered", show=False),
        Binding("r", "reload", show=False),
        Binding("q", "quit_guarded", show=False),
        Binding("escape", "escape", show=False),
        Binding("pagedown,ctrl+d", "page(1)", show=False, priority=True),
        Binding("pageup,ctrl+u", "page(-1)", show=False, priority=True),
    ]

    def __init__(self, ask_fn: AskFn | None = None, reload_every: float = RELOAD_S) -> None:
        """Load the store; ``ask_fn(prompt, cwd)`` answers "?" questions (tests pass a stub)."""
        super().__init__()
        self.ask_fn: AskFn = ask_fn or ask.ask_claude
        self.reload_every = reload_every
        self.decisions: list[Decision] = []
        self.by_id: dict[str, Decision] = {}
        self.tabs: list[str | None] = [None]
        self.tab_names: dict[str, str] = {}
        self.thread_info: dict[str, dict[str, Any]] = {}
        self.sig: tuple[int, float] = (-1, 0.0)
        self.ctx_cache: dict[str, tuple[str, list[list[str]]]] = {}
        self.tab_idx = 0
        self.show_answered = False
        self.filter_text = ""
        self.rows: list[Decision] = []
        self.last_batch: tuple[str, list[dict[str, Any]], dict[str, dict[str, Any]]] | None = None
        self.g_pending = False
        self.o_pending = False
        self.shown_id: str | None = None
        self.bridge = ""
        self.last_msg = ""
        self.submitted_log: list[tuple[str, str]] = []
        self.spin = 0
        self.load_store()

    # ---------- store ----------
    def load_store(self) -> bool:
        """(Re)read the store; update Decision objects in place so selection and cursors survive."""
        self.sig = store.signature()
        items = store.all_items()
        all_t = store.threads()
        persisted = store.load_staged()
        seen = set()
        for item in items:
            f = fields_from_item(item)
            d = self.by_id.get(item["id"])
            if d is None:
                d = Decision(id=item["id"], **f)
                d.cursor = d.pick
                self.decisions.append(d)
                self.by_id[d.id] = d
            else:
                for k, v in f.items():
                    setattr(d, k, v)
                d.cursor = min(d.cursor, d.n_rows)
            st = persisted["staged"].get(d.id)
            if st is not None and st.get("kind") == "option" and not 1 <= st.get("option", 0) <= len(d.options):
                st = None  # the options changed under the pick
            d.staged = st
            dr = persisted["drafts"].get(d.id, {})
            d.note_text, d.attach_note, d.unclear = (
                dr.get("note_text", ""),
                dr.get("attach_note", ""),
                dr.get("unclear", ""),
            )
            seen.add(d.id)
        self.decisions = [d for d in self.decisions if d.id in seen]
        self.by_id = {d.id: d for d in self.decisions}
        used = {d.thread for d in self.decisions}
        order = sorted(used, key=lambda s: (all_t.get(s, {}).get("created", ""), s))
        cur = self.tabs[self.tab_idx] if self.tab_idx < len(self.tabs) else None
        self.tabs = [None, *order]
        self.tab_idx = self.tabs.index(cur) if cur in self.tabs else 0
        self.tab_names = {s: all_t.get(s, {}).get("name") or s for s in used}
        self.thread_info = {s: all_t.get(s, {}) for s in used}
        return True

    def tname(self, slug: str) -> str:
        """Display name of a thread."""
        return self.tab_names.get(slug, slug)

    def persist_staged(self) -> None:
        """Write staged picks and typed drafts to the store."""
        staged = {d.id: d.staged for d in self.decisions if d.staged}
        drafts = {
            d.id: {"note_text": d.note_text, "attach_note": d.attach_note, "unclear": d.unclear}
            for d in self.decisions
            if d.note_text or d.attach_note or d.unclear
        }
        store.save_staged(staged, drafts)

    def busy(self) -> bool:
        """True while the user types or a modal is open (auto reload waits)."""
        return len(self.screen_stack) > 1 or isinstance(self.focused, Input)

    def auto_reload(self) -> None:
        """Timer: reload when the store changed on disk (a session added or answered something)."""
        if store.signature() == self.sig or self.busy():
            return
        n_before = len(self.decisions)
        self.load_store()
        try:
            self.refresh_all()
        except NoMatches:  # the timer can fire while the app is shutting down
            return
        added = len(self.decisions) - n_before
        if added > 0:
            self.notify(f"{added} new decision{'s' * (added != 1)}", timeout=3)

    def action_reload(self) -> None:
        """R reloads from disk now."""
        self.load_store()
        self.ctx_cache.clear()
        self.refresh_all()
        self.notify(f"Reloaded: {self.pending_in(None)} pending", timeout=1.5)

    # ---------- layout ----------
    def compose(self) -> ComposeResult:
        """Lay out the inbox exactly as the mock."""
        with Horizontal(id="top"):
            yield Static(id="title")
            yield Static(id="counts")
        yield Static(id="tabs")
        with Horizontal(id="main"):
            with Vertical(id="left"):
                yield Static(id="thread_head")
                yield Input(placeholder="filter: type to narrow, enter keep, esc clear", id="filter")
                yield DecisionList(id="list")
            with Vertical(id="right"):
                with DetailPane(id="detail"):
                    yield Static(id="bridge")
                    yield Static(id="detail_body")
                    yield Static(id="question")
                    for i in range(1, MAX_SLOTS + 1):
                        with Vertical(id=f"slot{i}", classes="slot hide"):
                            yield Static(id=f"opt{i}")
                            yield Input(id=f"in{i}")
                    yield Static(id="detail_tail")
                with Vertical(id="ctx"):
                    with Pane(id="ctx_scroll"):
                        yield Static(id="ctx_body")
                    yield Input(placeholder="ask: e.g. plain words? / if I do nothing? / why that pick?", id="ask")
        yield Static(id="footer")

    def on_mount(self) -> None:
        """Set titles, focus the list, start the reload and spinner timers."""
        self.query_one("#title", Static).update(Text.assemble(("DECISIONS", "bold"), (f"  {store.home()}", DIM)))
        self.query_one("#ctx").border_title = "CONTEXT · where this came from"
        self.query_one("#detail").border_title = "DETAIL"
        self.query_one("#list").focus()
        self.refresh_all()
        self.call_after_refresh(self.refresh_all)
        if self.reload_every > 0:
            self.set_interval(self.reload_every, self.auto_reload)
        self.set_interval(0.15, self.tick)

    def tick(self) -> None:
        """Spinner frame while a "?" question is in flight."""
        try:
            d = self.current()
            if d is not None and d.asking and self.query_one("#ctx").has_class("shown"):
                self.spin += 1
                self.query_one("#ctx_body", Static).update(self.context_text(d))
        except NoMatches:  # the timer can fire while the app is shutting down
            return

    def on_resize(self, event: events.Resize) -> None:
        """Redraw on resize."""
        self.call_after_refresh(self.refresh_all)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Keys do nothing under a modal or while typing (except esc and submit)."""
        if len(self.screen_stack) > 1:
            return False
        if isinstance(self.focused, Input) and action not in ("escape", "submit"):
            return False
        return True

    # ---------- data helpers ----------
    def pending_in(self, thread: str | None) -> int:
        """Open decisions in a thread (None = all)."""
        return sum(1 for d in self.decisions if d.status in OPEN and (thread is None or d.thread == thread))

    def staged_in(self, thread: str | None) -> int:
        """Staged decisions in a thread (None = all)."""
        return sum(1 for d in self.decisions if d.staged and (thread is None or d.thread == thread))

    def unstaged_in(self, thread: str | None) -> int:
        """Open decisions not yet staged."""
        return self.pending_in(thread) - self.staged_in(thread)

    def staged_list(self) -> list[Decision]:
        """Staged decisions in tab order."""
        order = {t: i for i, t in enumerate(self.tabs)}
        return sorted((d for d in self.decisions if d.staged), key=lambda d: order.get(d.thread, 99))

    def compute_visible(self) -> list[Decision]:
        """Rows of the current tab after the filter."""
        t = self.tabs[self.tab_idx]
        f = self.filter_text.lower()
        rows = [
            d
            for d in self.decisions
            if (t is None or d.thread == t)
            and (self.show_answered or d.status in OPEN)
            and (not f or f in f"{d.title} {d.question} {self.tname(d.thread)} {d.kind}".lower())
        ]
        rows.sort(key=lambda d: (d.status not in OPEN, -d.urgency, -d.asked, -d.age_days))
        return rows

    def current(self) -> Decision | None:
        """The highlighted decision."""
        idx = self.query_one("#list", DecisionList).highlighted
        if idx is None or idx >= len(self.rows):
            return None
        return self.rows[idx]

    # ---------- rendering ----------
    def refresh_all(self, select_id: str | None = None) -> None:
        """Redraw everything."""
        self.render_counts()
        self.render_tabs()
        self.render_list(select_id)
        self.render_footer()
        self.show_selected()

    def render_counts(self) -> None:
        """Top-right counts."""
        n = sum(d.status in OPEN for d in self.decisions)
        s = self.staged_in(None)
        m = sum(d.status in ("answered", "handed", "dropped") for d in self.decisions)
        k = sum(d.status == "parked" for d in self.decisions)
        t = Text.assemble(
            ("pending ", DIM),
            (str(n), "bold"),
            ("  ·  staged ", DIM),
            (str(s), f"bold {STAGED}" if s else "bold"),
            ("  ·  answered ", DIM),
            (str(m), "bold"),
            ("  ·  parked ", DIM),
            (str(k), "bold"),
        )
        self.query_one("#counts", Static).update(t)

    def render_tabs(self) -> None:
        """Tab strip: a window of whole tabs that always holds the current one."""
        width = max(self.size.width - 6, 20)
        segs: list[Text] = []
        for i, t in enumerate(self.tabs):
            p, st = self.pending_in(t), self.staged_in(t)
            name = "All" if t is None else self.tname(t)
            seg = Text()
            cur = i == self.tab_idx
            seg.append(str((i + 1) % 10), style=f"bold {ACCENT}" if cur else DIM)
            base = f"bold {BG} on {ACCENT}" if cur else (DIM if (p == 0 and t is not None) else "")
            if p == 0 and t is not None:
                seg.append(f" ✓ {name} ", style=base)
            else:
                seg.append(f" {name} ({p}", style=base)
                if st:
                    seg.append(f" · {st} staged", style=f"bold {BG} on {ACCENT}" if cur else f"bold {STAGED}")
                seg.append(") ", style=base)
            seg.append("  ")
            segs.append(seg)
        start = 0
        while sum(s.cell_len for s in segs[start : self.tab_idx + 1]) > width and start < self.tab_idx:
            start += 1
        end, used = start, 0
        while end < len(segs) and used + segs[end].cell_len <= width:
            used += segs[end].cell_len
            end += 1
        text = Text()
        if start > 0:
            text.append("‹ ", style=f"bold {ACCENT}")
        for s in segs[start:end]:
            text.append_text(s)
        if end < len(segs):
            text.append("›", style=f"bold {ACCENT}")
        self.query_one("#tabs", Static).update(text)

    def row_text(self, d: Decision, width: int) -> Text:
        """Two-line list row."""
        if d.staged:
            glyph, gstyle, tstyle = STAGED_GLYPH, f"bold {STAGED}", f"bold {STAGED}"
        elif d.status == "explaining":
            glyph, gstyle, tstyle = "?", f"bold {EXPLAIN}", EXPLAIN
        elif d.status == "pending":
            glyph, gstyle, tstyle = GLYPH["pending"], "", ""
        else:
            glyph, gstyle = GLYPH[d.status], "dim"
            tstyle = "dim strike" if d.status in ("dropped", "closed") else "dim"
        title = d.title if len(d.title) <= width - 3 else d.title[: max(width - 4, 1)] + "…"
        t = Text()
        t.append(f" {glyph} ", style=gstyle)
        t.append(title, style=tstyle)
        t.append("\n   ")
        meta = []
        if d.staged:
            tag = stage_tag(d.staged)
            t.append(tag, style=STAGED)
            meta_room = width - 3 - len(tag)
        else:
            meta_room = width - 3
            if d.status == "explaining":
                meta.append("asked Claude to explain")
            if d.status == "closed":
                meta.append("closed by Claude")
        meta += [f"[{KIND_SHORT.get(d.kind, d.kind)}]", age_str(d.age_days)]
        if d.asked >= 3:
            meta.append(f"asked x{d.asked}")
        if d.status == "parked" and d.until:
            meta.append(f"until {d.until}")
        if self.tabs[self.tab_idx] is None:
            meta.append(self.tname(d.thread))
        m = (" · " if d.staged else "") + " · ".join(meta)
        if len(m) > meta_room:
            m = m[: max(meta_room - 1, 0)] + "…"
        t.append(m, style=EXPLAIN if d.status == "explaining" and not d.staged else "dim")
        return t

    def render_list(self, select_id: str | None = None) -> None:
        """Left list, its border title and the thread goal/state header."""
        ol = self.query_one("#list", DecisionList)
        cur = self.current()
        keep = select_id or (cur.id if cur else None)
        self.rows = self.compute_visible()
        width = max(ol.size.width - 2, 20)
        ol.clear_options()
        ol.add_options([Option(self.row_text(d, width), id=d.id) for d in self.rows])
        ids = [d.id for d in self.rows]
        if ids:
            ol.highlighted = ids.index(keep) if keep in ids else 0
        t = self.tabs[self.tab_idx]
        name = "All threads" if t is None else self.tname(t)
        extra = []
        if self.filter_text:
            extra.append(f"filter '{self.filter_text}'")
        if self.show_answered:
            extra.append("showing answered")
        left = self.query_one("#left")
        st = self.staged_in(t)
        left.border_title = f"{name} · {self.pending_in(t)} pending" + (f" · {st} staged" if st else "")
        left.border_subtitle = " · ".join(extra)
        head = self.query_one("#thread_head", Static)
        info = self.thread_info.get(t or "", {})
        if t is not None and (info.get("goal") or info.get("state")):
            state = "done" if self.pending_in(t) == 0 else info.get("state", "")
            h = Text()
            h.append("Goal  ", style=DIM)
            h.append(info.get("goal", "") + "\n")
            h.append("Now   ", style=DIM)
            h.append(state, style=f"bold {ACCENT}" if state == "blocked on you" else "bold")
            if info.get("state_note"):
                h.append(f" · {info['state_note']}", style=SOFT)
            head.update(h)
            head.add_class("shown")
        else:
            head.remove_class("shown")

    def render_footer(self) -> None:
        """Key help, or the g/o prompt."""
        t = Text()
        if self.g_pending:
            t.append("g: press a tab number (1-9, 0 = 10th) · esc cancel", style=f"bold {ACCENT}")
        elif self.o_pending:
            t.append("o: press a linked-decision number (1-9) · esc cancel", style=f"bold {ACCENT}")
        else:
            s = self.staged_in(None)
            if s:
                t.append(f" s submit ({s} staged) ", style=f"bold {BG} on {STAGED}")
            else:
                t.append("s submit (0 staged)", style=DIM)
            t.append("  ")
            if self.last_msg:
                t.append(self.last_msg, style=f"bold {ACCENT}")
                t.append("   ")
            keys = [
                ("j/k", "list"),
                ("h/l", "tabs"),
                ("J/K 1-9", "option"),
                ("⏎", "stage"),
                ("n", "note"),
                ("u", "undo"),
                ("p", "park"),
                ("x", "drop"),
                ("y", "you pick"),
                ("o#", "linked"),
                ("c", "context"),
                ("?", "ask"),
                ("/", "filter"),
                ("a", "answered"),
                ("g#", "tab #"),
                ("r", "reload"),
                ("q", "quit"),
            ]
            for k, v in keys:
                t.append(k, style="bold")
                t.append(f" {v}  ", style=DIM)
        self.query_one("#footer", Static).update(t)

    def default_cursor(self, d: Decision) -> int:
        """Where the option cursor starts: the staged row, else Claude's star."""
        st = d.staged
        if st and st["kind"] == "option":
            return st["option"]
        if st and st["kind"] == "note":
            return d.note_row
        if st and st["kind"] == "explain":
            return d.explain_row
        return d.pick

    def show_selected(self) -> None:
        """Redraw the detail pane for the highlighted decision."""
        d = self.current()
        if d is not None and d.id != self.shown_id:
            prev = self.by_id.get(self.shown_id or "")
            self.bridge = self.bridge_for(prev, d)
            d.cursor = self.default_cursor(d)
            self.query_one("#detail", DetailPane).scroll_home(animate=False)
            if self.query_one("#ctx").has_class("shown"):
                self.load_context(d)
        self.shown_id = d.id if d else None
        bridge = self.query_one("#bridge", Static)
        bridge.update(self.bridge)
        bridge.set_class(bool(self.bridge) and d is not None, "shown")
        body = self.query_one("#detail_body", Static)
        if d is None:
            if not self.decisions:
                msg = Text("\n  No pending decisions.\n", style="bold")
                msg.append("\n  Sessions add them with `houston add`; they appear here on their own.", style=DIM)
            else:
                msg = Text("\n  Nothing pending here. ✓\n", style="bold")
                msg.append("\n  l next tab · a show answered", style=DIM)
            body.update(msg)
            self.query_one("#question", Static).update("")
            self.query_one("#detail_tail", Static).update("")
            for i in range(1, MAX_SLOTS + 1):
                self.query_one(f"#slot{i}").add_class("hide")
            self.query_one("#ctx_body", Static).update("")
            return
        body.update(self.background_text(d))
        self.query_one("#question", Static).update(Text(d.question, style="bold"))
        self.render_options(d)
        self.query_one("#detail_tail", Static).update(self.tail_text(d))
        self.query_one("#ctx_body", Static).update(self.context_text(d))

    def linked(self, prev: Decision, d: Decision) -> str:
        """How d relates to prev, from either side's links ('' if not linked)."""
        for ln in d.links:
            if ln["to"] == prev.id:
                return f"after “{prev.title}”, this one: {ln['rel']}"
        for ln in prev.links:
            if ln["to"] == d.id:
                return f"“{prev.title}” → this one: {ln['rel']}"
        return ""

    def bridge_for(self, prev: Decision | None, d: Decision) -> str:
        """The "Next in <thread>" line between linked decisions."""
        if prev is None or prev.thread != d.thread or prev.id == d.id:
            return ""
        rel = self.linked(prev, d)
        return f"Next in {self.tname(d.thread)}: {rel}" if rel else ""

    def background_text(self, d: Decision) -> Text:
        """Header, staged/answered banner, story, changes and links."""
        t = Text()
        head = [self.tname(d.thread).upper(), KIND_SHORT.get(d.kind, d.kind), f"{age_str(d.age_days)} old"]
        if d.asked > 1:
            head.append(f"asked x{d.asked}")
        t.append(" · ".join(head) + "\n", style=DIM)
        if d.staged:
            t.append(f"{STAGED_GLYPH} Staged: {stage_line(d.staged)}", style=f"bold {STAGED}")
            t.append("   (u unstage · s submit)\n", style=DIM)
        if d.status == "explaining":
            t.append("? Asked Claude to explain", style=f"bold {EXPLAIN}")
            if d.answer:
                t.append(f": “{d.answer}”", style=EXPLAIN)
            t.append("\n  Claude will rewrite this decision with more context.\n", style=EXPLAIN)
        elif d.status == "closed":
            t.append(f"{GLYPH['closed']} {d.answer[0].upper() + d.answer[1:]}\n", style="bold")
        elif d.status == "parked":
            t.append(f"{GLYPH['parked']} Parked: until {d.until}", style="bold")
            t.append("   (p unparks now)\n", style=DIM)
        elif d.status != "pending":
            word = {"answered": "Answered", "dropped": "Dropped", "handed": "Handed to Claude"}
            t.append(f"{GLYPH[d.status]} {word[d.status]}: {d.answer}\n", style="bold")
        if d.story:
            t.append("\nThe story so far\n", style=f"bold {SOFT}")
            for line in d.story:
                t.append(f" · {line}\n", style=SOFT)
        if d.changed:
            t.append("What changed since you last looked\n", style=f"bold {SOFT}")
            for line in d.changed:
                t.append(f" · {line}\n", style=SOFT)
        if d.links:
            t.append("Linked decisions", style=f"bold {SOFT}")
            t.append("  (o + number to jump)\n", style=DIM)
            for i, ln in enumerate(d.links[:9], 1):
                other = self.by_id.get(ln["to"])
                if other is None:
                    continue
                t.append(f" o{i} ", style="bold")
                g = STAGED_GLYPH if other.staged else GLYPH[other.status]
                t.append(f"{g} {other.title}", style="" if other.status in OPEN else "dim")
                t.append(f" — {ln['rel']}\n", style=DIM)
        return t

    def opt_text(self, d: Decision, i: int, o: dict[str, Any], expanded: bool) -> Text:
        """One option row; expanded rows show means / next steps / cost / risk / why-pick."""
        star = i == d.pick
        st = d.staged
        staged_here = bool(st and st["kind"] == "option" and st["option"] == i)
        chosen = d.status == "answered" and d.answered_option == i
        t = Text()
        if staged_here:
            mark, style = f"{STAGED_GLYPH} ", f"bold {STAGED}"
        elif star:
            mark, style = "★ ", f"bold {ACCENT}"
        else:
            mark, style = ("✓ " if chosen else "  "), "bold"
        t.append(mark, style=style)
        t.append(f"{i}  {o['label']}", style=style)
        if star and staged_here:
            t.append(" ★", style=f"bold {ACCENT}")
        t.append(f"  —  {o['then']}", style=SOFT if expanded else DIM)
        if staged_here and st:
            t.append("\n     staged", style=f"bold {STAGED}")
            if st.get("note"):
                t.append(f" · note: “{st['note']}”", style=STAGED)
        if not expanded:
            return t
        rows = [
            ("What this means", o.get("means")),
            ("What happens next", o.get("next_steps")),
            ("Cost / time", o.get("cost")),
            ("Risk / you give up", o.get("risk")),
        ]
        if star:
            rows.append(("Why Claude picks this", o.get("why_pick")))
        for label, val in rows:
            if not val:
                continue
            t.append(f"\n\n   {label}\n", style=f"bold {ACCENT}" if label.startswith("Why") else "bold")
            if isinstance(val, list):
                for n, step in enumerate(val, 1):
                    t.append(f"   {n}. {step}\n" if n < len(val) else f"   {n}. {step}")
            else:
                t.append(f"   {val}")
        t.append("\n\n   enter = stage this option · n = add a note to this pick ↓", style=DIM)
        return t

    def note_row_text(self, d: Decision, expanded: bool) -> Text:
        """The "Write my own answer" row."""
        staged_here = bool(d.staged and d.staged["kind"] == "note")
        t = Text()
        style = f"bold {STAGED}" if staged_here else "bold"
        t.append(f"{STAGED_GLYPH} " if staged_here else "✎ ", style=style)
        t.append(f"{d.note_row}  Write my own answer", style=style)
        if staged_here:
            t.append("  staged", style=f"bold {STAGED}")
        if d.note_text and not expanded:
            t.append(f"\n     “{d.note_text}”", style=STAGED if staged_here else SOFT)
        if expanded:
            t.append("\n\n   Type the answer in your words. Claude reads it as the decision.", style=SOFT)
            t.append("\n   enter = type · enter again = stage · esc = leave the box (text is kept)", style=DIM)
        return t

    def explain_row_text(self, d: Decision, expanded: bool) -> Text:
        """The "Explain this better" row."""
        staged_here = bool(d.staged and d.staged["kind"] == "explain")
        t = Text()
        style = f"bold {STAGED}" if staged_here else f"bold {EXPLAIN}"
        t.append(f"{STAGED_GLYPH} " if staged_here else "? ", style=style)
        t.append(f"{d.explain_row}  Explain this better / I need more context", style=style)
        if staged_here:
            t.append("  staged", style=f"bold {STAGED}")
        if d.unclear and not expanded:
            t.append(f"\n     unclear: “{d.unclear}”", style=STAGED if staged_here else SOFT)
        if expanded:
            t.append("\n\n   Not an answer. Claude rewrites this decision with more context;", style=SOFT)
            t.append("\n   it stays in your queue.", style=SOFT)
            t.append("\n   enter = type what is unclear (optional) · enter again = stage", style=DIM)
        return t

    def slot_input_value(self, d: Decision, i: int) -> str:
        """Text kept for the input box of row i."""
        if i == d.note_row:
            return d.note_text
        if i == d.explain_row:
            return d.unclear
        return d.attach_note

    def render_options(self, d: Decision) -> None:
        """Draw the option rows and the two fixed rows."""
        for i in range(1, MAX_SLOTS + 1):
            slot = self.query_one(f"#slot{i}")
            if i > d.n_rows:
                slot.add_class("hide")
                continue
            cur = i == d.cursor
            st = d.staged
            if i <= len(d.options):
                text = self.opt_text(d, i, d.options[i - 1], cur)
                staged_here = bool(st and st["kind"] == "option" and st["option"] == i)
                ph = "add a note to this pick (optional) — n or click to type"
            elif i == d.note_row:
                text = self.note_row_text(d, cur)
                staged_here = bool(st and st["kind"] == "note")
                ph = "your own answer…"
            else:
                text = self.explain_row_text(d, cur)
                staged_here = bool(st and st["kind"] == "explain")
                ph = "what is unclear? (optional)"
            self.query_one(f"#opt{i}", Static).update(text)
            inp = self.query_one(f"#in{i}", Input)
            inp.placeholder = ph
            if not inp.has_focus:
                inp.value = self.slot_input_value(d, i)
            slot.remove_class("hide")
            slot.set_class(cur, "cur")
            slot.set_class(staged_here, "staged")

    def tail_text(self, d: Decision) -> Text:
        """Key hint, what waits on this, and where it came from."""
        t = Text()
        t.append("J/K or 1-9 move · enter stage · p park · x drop · y you pick · s submit\n", style=DIM)
        t.append("\nWaits on this:\n", style="bold")
        for w in d.waits_on or ["nothing else"]:
            t.append(f"   · {w}\n")
        t.append("\nFrom:\n", style="bold")
        s = d.src
        t.append(f"   {s.get('date', '?')} · session {str(s.get('session_id', '?'))[:8]}")
        if s.get("branch"):
            t.append(f" · {s['branch']}")
        t.append("\n")
        if s.get("pointer"):
            t.append(f"   {s['pointer']}\n", style=DIM)
        if s.get("cwd"):
            t.append(f"   {s['cwd']}\n", style=DIM)
        return t

    def context_text(self, d: Decision) -> Text:
        """Source conversation around the ask, then the question history."""
        t = Text()
        s = d.src
        t.append(f"session {s.get('date', '?')} · {s.get('session_id', '?')}\n", style=DIM)
        got = self.ctx_cache.get(d.id)
        if got is None:
            t.append("\n   loading the source conversation…\n", style=DIM)
        else:
            where, turns = got
            label = {
                "transcript": "from the session transcript",
                "excerpt": "stored excerpt (transcript not found)",
                "none": "source not found",
            }[where]
            t.append(f"{label}\n\n", style=DIM)
            for who, line in turns:
                t.append(f"{who:<10}", style="bold" if who == "USER" else DIM)
                t.append(line + "\n", style="" if who == "USER" else SOFT)
        t.append("\nQuestions", style="bold")
        t.append("  (? to ask)\n", style=DIM)
        if not d.history and not d.asking:
            t.append("   none yet\n", style=DIM)
        for q, a in d.history:
            t.append("   you     ", style="bold")
            t.append(q + "\n")
            t.append("   answer  ", style=DIM)
            t.append(a + "\n\n", style=SOFT)
        if d.asking:
            t.append("   you     ", style="bold")
            t.append(d.asking + "\n")
            t.append(f"   {SPIN[self.spin % len(SPIN)]}  asking Claude (sonnet)…\n", style=ACCENT)
        return t

    def load_context(self, d: Decision) -> None:
        """Read the source transcript in a worker thread (files can be 40 MB) and cache it."""
        if d.id in self.ctx_cache:
            return
        item = d.item

        def work() -> None:
            """Stream the transcript off the UI thread."""
            try:
                got = store.context_for(item)
            except Exception as e:  # noqa: BLE001 - shown to the user, never crashes the UI
                got = ("none", [["ERROR", f"could not read the transcript: {e}"]])
            self.call_from_thread(self.context_loaded, d.id, got)

        self.run_worker(work, thread=True, group="ctx")

    def context_loaded(self, did: str, got: tuple[str, list[list[str]]]) -> None:
        """Show the context once loaded."""
        self.ctx_cache[did] = got
        cur = self.current()
        if cur is not None and cur.id == did:
            self.query_one("#ctx_body", Static).update(self.context_text(cur))

    # ---------- events ----------
    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        """Moving in the list redraws the detail pane."""
        if event.option_list.id == "list" and event.option_id != self.shown_id:
            self.show_selected()

    def slot_of(self, inp: Input) -> int | None:
        """Row number of an option input box (None for other inputs)."""
        if inp.id and inp.id.startswith("in") and inp.id[2:].isdigit():
            return int(inp.id[2:])
        return None

    def on_input_changed(self, event: Input.Changed) -> None:
        """Keep typed text on the decision; the filter narrows live."""
        if event.input.id == "filter":
            self.filter_text = event.value.strip()
            self.render_list()
            self.show_selected()
            return
        i = self.slot_of(event.input)
        d = self.by_id.get(self.shown_id or "")
        if i is None or d is None or not event.input.has_focus:
            return
        if i == d.note_row:
            d.note_text = event.value
        elif i == d.explain_row:
            d.unclear = event.value
        else:
            d.attach_note = event.value

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Enter in a box: keep the filter, ask a question, or stage."""
        if event.input.id == "filter":
            self.query_one("#list").focus()
            if not self.filter_text:
                event.input.remove_class("shown")
            return
        if event.input.id == "ask":
            d = self.by_id.get(self.shown_id or "")
            q = event.value.strip()
            event.input.value = ""
            event.input.remove_class("shown")
            self.query_one("#list").focus()
            if d and q:
                self.start_ask(d, q)
            return
        i = self.slot_of(event.input)
        d = self.current()
        if i is None or d is None:
            return
        self.query_one("#list").focus()
        if i == d.note_row:
            d.note_text = event.value.strip()
            if not d.note_text:
                self.notify("Type an answer first", severity="warning", timeout=2)
                self.render_options(d)
                return
            self.stage(d, {"kind": "note", "note": d.note_text})
        elif i == d.explain_row:
            d.unclear = event.value.strip()
            self.stage(d, {"kind": "explain", "note": d.unclear})
        else:
            d.attach_note = event.value.strip()
            self.stage_option(d, i)

    def start_ask(self, d: Decision, q: str) -> None:
        """Send one question to the headless model in a worker thread; the UI keeps running."""
        if d.asking:
            self.notify("A question on this decision is still running", timeout=2)
            return
        d.asking = q
        self.show_selected()
        self.query_one("#ctx_scroll", Pane).scroll_end(animate=False)
        item, ask_fn = d.item, self.ask_fn

        def work() -> None:
            """Build the prompt (with the transcript excerpt) and ask."""
            try:
                where, turns = self.ctx_cache.get(d.id) or store.context_for(item)
                prompt = ask.build_prompt(item, where, turns, q)
                cwd = (item.get("source") or {}).get("cwd")
                answer = ask_fn(prompt, cwd)
            except Exception as e:  # noqa: BLE001 - fail soft with a clear line
                answer = f"(no answer: {e})"
            self.call_from_thread(self.finish_ask, d.id, q, answer)

        self.run_worker(work, thread=True, group="ask")

    def finish_ask(self, did: str, q: str, answer: str) -> None:
        """Store the Q&A on the decision file and show it."""
        d = self.by_id.get(did)
        if d is None:
            return
        d.asking = ""
        d.history.append((q, answer))
        try:
            store.add_qa(did, q, answer)
        except store.StoreError as e:
            self.notify(f"answer not saved: {e}", severity="warning", timeout=3)
        if self.shown_id == did:
            self.query_one("#ctx_body", Static).update(self.context_text(d))
            self.query_one("#ctx_scroll", Pane).scroll_end(animate=False)

    # ---------- staging ----------
    def stage_option(self, d: Decision, i: int) -> None:
        """Stage option i (the claude_decided "explain" option stages an explain request)."""
        label = d.options[i - 1]["label"]
        if d.kind == "claude_decided" and label == "explain":
            self.stage(d, {"kind": "explain", "note": d.unclear})
            return
        self.stage(d, {"kind": "option", "option": i, "label": label, "note": d.attach_note.strip()})

    def stage(self, d: Decision, st: dict[str, Any]) -> None:
        """Stage a choice (persisted at once) and move on."""
        ids_before = [x.id for x in self.rows]
        idx = ids_before.index(d.id) if d.id in ids_before else 0
        replaced = d.staged is not None
        d.staged = st
        self.persist_staged()
        self.last_msg = f"{'Restaged' if replaced else 'Staged'}: {d.title} {stage_tag(st)} — u unstage · s submit"
        self.notify(self.last_msg, timeout=2)
        self.advance(ids_before, idx)

    def advance(self, ids_before: list[str], idx: int) -> None:
        """Move to the next unstaged pending decision; change tab when this one has none left."""

        def open_unstaged(i: str) -> bool:
            """True for an open decision with nothing staged."""
            x = self.by_id[i]
            return x.status in OPEN and not x.staged

        t = self.tabs[self.tab_idx]
        if t is not None and self.unstaged_in(t) == 0:
            n = len(self.tabs)
            for step in range(1, n + 1):
                j = (self.tab_idx + step) % n
                if self.unstaged_in(self.tabs[j]) > 0:
                    self.notify(f"{self.tname(t)}: all staged", timeout=2)
                    self.tab_idx = j
                    break
            self.refresh_all()
            self.select_first_open()
            return
        order = ids_before[idx + 1 :] + ids_before[:idx]
        nxt = next((i for i in order if open_unstaged(i)), None)
        self.refresh_all(select_id=nxt)

    def select_first_open(self) -> None:
        """Highlight the first open, unstaged row."""
        for i, d in enumerate(self.rows):
            if d.status in OPEN and not d.staged:
                self.query_one("#list", DecisionList).highlighted = i
                break
        self.show_selected()

    def do_submit(self) -> int:
        """Write every staged choice to the store as one batch."""
        staged = self.staged_list()
        if not staged:
            return 0
        payload = {}
        for d in staged:
            st = dict(d.staged or {})
            if st["kind"] == "park" and not (st.get("until_date") or st.get("until_event")):
                st.update(park_target(d, st["until"]))
            payload[d.id] = st
        try:
            batch, snaps = store.submit_batch(payload)
        except store.StoreError as e:
            self.notify(f"Submit failed, nothing changed: {e}", severity="error", timeout=5)
            return 0
        self.last_batch = (batch, snaps, {d.id: dict(d.staged or {}) for d in staged})
        for d in staged:
            self.submitted_log.append((d.title, stage_line(d.staged or {})))
            d.staged = None
        self.persist_staged()
        self.load_store()
        n = len(payload)
        self.last_msg = f"Submitted {n} decisions — u undoes the batch"
        self.notify(f"Submitted {n} decisions", timeout=3)
        self.refresh_all()
        self.select_first_open()
        return n

    # ---------- actions ----------
    def action_tab(self, step: int) -> None:
        """H/l change tab."""
        self.set_tab((self.tab_idx + step) % len(self.tabs))

    def set_tab(self, i: int) -> None:
        """Go to tab i."""
        if 0 <= i < len(self.tabs):
            self.tab_idx = i
            self.refresh_all()
            self.select_first_open()

    def action_jump(self, n: int) -> None:
        """Alt+digit or g+digit jumps to a tab."""
        self.set_tab(9 if n == 0 else n - 1)

    def action_g(self) -> None:
        """G waits for a tab number."""
        self.g_pending = True
        self.render_footer()

    def action_digit(self, n: int) -> None:
        """Digits: tab after g, link after o, else the option cursor."""
        if self.g_pending:
            self.g_pending = False
            self.render_footer()
            self.action_jump(n)
        elif self.o_pending:
            self.o_pending = False
            self.render_footer()
            self.jump_link(n)
        elif n:
            self.set_cursor(n)

    def set_cursor(self, n: int) -> None:
        """Move the option cursor to row n."""
        d = self.current()
        if d is None:
            return
        if not 1 <= n <= d.n_rows:
            self.notify(f"No option {n} here", severity="warning", timeout=2)
            return
        d.cursor = n
        self.render_options(d)
        self.call_after_refresh(self.query_one(f"#slot{n}").scroll_visible, animate=False)

    def action_opt(self, step: int) -> None:
        """J/K step the option cursor."""
        d = self.current()
        if d:
            self.set_cursor(max(1, min(d.n_rows, d.cursor + step)))

    def action_page(self, step: int) -> None:
        """Page keys scroll the detail pane."""
        pane = self.query_one("#detail", DetailPane)
        pane.scroll_page_down(animate=False) if step > 0 else pane.scroll_page_up(animate=False)

    def action_o(self) -> None:
        """O waits for a link number."""
        d = self.current()
        if d is None or not d.links:
            self.notify("No linked decisions here", timeout=2)
            return
        self.o_pending = True
        self.render_footer()

    def jump_link(self, n: int) -> None:
        """Go to linked decision n."""
        d = self.current()
        if d is None or not 1 <= n <= len(d.links):
            self.notify(f"No link o{n} here", severity="warning", timeout=2)
            return
        target = self.by_id.get(d.links[n - 1]["to"])
        if target is None:
            self.notify("That linked decision is not in the store", severity="warning", timeout=2)
            return
        if target.status not in OPEN and not self.show_answered:
            self.show_answered = True
            self.notify("showing answered too (the link target is closed)", timeout=2)
        if self.tabs[self.tab_idx] not in (None, target.thread):
            self.tab_idx = self.tabs.index(target.thread)
        self.filter_text = ""
        self.query_one("#filter", Input).value = ""
        self.refresh_all(select_id=target.id)

    def action_move(self, step: int) -> None:
        """J/k move in the list."""
        ol = self.query_one("#list", DecisionList)
        ol.action_cursor_down() if step > 0 else ol.action_cursor_up()

    def focus_slot_input(self, d: Decision, i: int) -> None:
        """Open the input box of row i."""
        inp = self.query_one(f"#in{i}", Input)
        inp.value = self.slot_input_value(d, i)
        inp.focus()
        self.call_after_refresh(inp.scroll_visible, animate=False)

    def action_accept(self) -> None:
        """Enter: stage the option, or open the box of a fixed row."""
        d = self.current()
        if d is None:
            return
        if d.cursor in (d.note_row, d.explain_row):
            self.focus_slot_input(d, d.cursor)
        else:
            self.stage_option(d, d.cursor)

    def action_note(self) -> None:
        """N types a note on the row under the cursor."""
        d = self.current()
        if d:
            self.focus_slot_input(d, d.cursor)

    def action_park(self) -> None:
        """P stages a park (or unparks a parked decision at once)."""
        d = self.current()
        if d is None:
            return
        if d.status == "parked":
            store.unpark(d.id)
            self.load_store()
            self.last_msg = f"Unparked: {d.title}"
            self.notify(self.last_msg, timeout=2)
            self.refresh_all(select_id=d.id)
            return

        def done(until: str | None) -> None:
            """Stage the chosen park."""
            if until:
                self.stage(d, {"kind": "park", "until": until, **park_target(d, until)})

        self.push_screen(ParkScreen(), done)

    def action_drop(self) -> None:
        """X stages a drop."""
        d = self.current()
        if d:
            self.stage(d, {"kind": "drop"})

    def action_handoff(self) -> None:
        """Y stages "you pick" (Claude decides; its star is the default)."""
        d = self.current()
        if d:
            self.stage(d, {"kind": "you_pick", "option": d.pick, "label": d.options[d.pick - 1]["label"]})

    def action_undo(self) -> None:
        """U unstages the current decision, else undoes the last submitted batch."""
        d = self.current()
        if d is not None and d.staged:
            d.staged = None
            self.persist_staged()
            self.last_msg = f"↶ unstaged: {d.title}"
            self.notify(self.last_msg, timeout=2)
            self.refresh_all(select_id=d.id)
            return
        if self.last_batch:
            batch, snaps, staged = self.last_batch
            store.undo_batch(batch, snaps)
            for did, st in staged.items():
                if did in self.by_id:
                    self.by_id[did].staged = st
            self.persist_staged()
            self.load_store()
            self.submitted_log = self.submitted_log[: -len(staged)]
            self.last_msg = f"↶ batch undone: {len(staged)} back to staged"
            self.notify(self.last_msg, timeout=2)
            self.last_batch = None
            self.refresh_all(select_id=next(iter(staged), None))
            return
        self.notify("Nothing to undo", timeout=2)

    def action_submit(self) -> None:
        """S opens the review screen; enter there submits all."""
        if isinstance(self.focused, Input):
            self.query_one("#list").focus()
        if not self.staged_list():
            self.notify("Nothing staged yet — enter stages a choice", timeout=2)
            return

        def done(ok: bool | None) -> None:
            """Submit or go back."""
            if ok:
                self.do_submit()
            else:
                self.refresh_all()

        self.push_screen(ReviewScreen(self), done)

    def action_toggle_context(self) -> None:
        """C shows or hides the source context."""
        ctx = self.query_one("#ctx")
        ctx.toggle_class("shown")
        if ctx.has_class("shown"):
            d = self.current()
            if d is not None:
                self.load_context(d)
        else:
            self.query_one("#ask").remove_class("shown")
            self.query_one("#list").focus()

    def action_ask(self) -> None:
        """? opens the ask box under the context."""
        d = self.current()
        if d is None:
            return
        self.query_one("#ctx").add_class("shown")
        self.load_context(d)
        ask_box = self.query_one("#ask", Input)
        ask_box.add_class("shown")
        ask_box.focus()

    def action_filter(self) -> None:
        """/ opens the filter."""
        f = self.query_one("#filter", Input)
        f.add_class("shown")
        f.focus()

    def action_toggle_answered(self) -> None:
        """A shows or hides answered, parked and closed decisions."""
        self.show_answered = not self.show_answered
        self.refresh_all()
        self.notify("showing answered too" if self.show_answered else "pending only", timeout=1.5)

    def action_escape(self) -> None:
        """Esc cancels g/o, leaves a box (text kept), or clears the filter."""
        if self.g_pending or self.o_pending:
            self.g_pending = self.o_pending = False
            self.render_footer()
            return
        f = self.focused
        if isinstance(f, Input) and self.slot_of(f) is not None:
            self.query_one("#list").focus()
            self.persist_staged()  # keep the typed draft across a restart
            d = self.current()
            if d:
                self.render_options(d)
            return
        ask_box = self.query_one("#ask", Input)
        flt = self.query_one("#filter", Input)
        if ask_box.has_class("shown"):
            ask_box.remove_class("shown")
        elif flt.has_class("shown") or self.filter_text:
            flt.value = ""
            flt.remove_class("shown")
            self.filter_text = ""
            self.refresh_all()
        self.query_one("#list").focus()

    def summary(self) -> str:
        """Text printed after quit."""
        lines = [f"Decision inbox: {len(self.submitted_log)} submitted this session."]
        lines += [f"  ✓ {title} → {what}" for title, what in self.submitted_log]
        lines.append(f"Store: {store.home()}" if self.submitted_log else "Nothing submitted.")
        return "\n".join(lines)

    def action_quit_guarded(self) -> None:
        """Q quits; with staged picks it asks first."""
        n = self.staged_in(None)
        if not n:
            self.exit(result=self.summary())
            return

        def done(what: str | None) -> None:
            """Submit, discard and quit, or cancel."""
            if what == "submit":
                self.action_submit()
            elif what == "discard":
                for d in self.decisions:
                    d.staged = None
                self.persist_staged()
                self.exit(result=self.summary() + f"\n({n} staged decisions discarded)")

        self.push_screen(QuitScreen(n), done)


if __name__ == "__main__":
    result = DecideApp().run()
    if result:
        print(result)
