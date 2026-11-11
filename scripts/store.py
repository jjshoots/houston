"""The decision store: one JSON file per decision under ``$HOUSTON_HOME`` (default ``~/.claude/houston``).

Layout::

    items/<id>.json   one decision each (written atomically: temp file + rename)
    threads.json      thread slug -> name / goal / state
    staged.json       picks staged in the inbox but not yet submitted (+ typed drafts)
    events.jsonl      append-only audit trail
    .lock             flock taken around every read-modify-write

Stdlib only, so the hooks and the CLI run under plain ``python3``.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import difflib
import fcntl
import glob
import json
import os
import re
import subprocess
import tempfile
import time
import uuid
from collections import deque
from collections.abc import Iterator
from pathlib import Path
from typing import Any

SCHEMA = 1
KINDS = ("spend", "go_ahead", "ruling", "scope", "design", "tell_someone", "claude_decided")
OPEN_STATES = ("pending", "explaining")  # both still wait on the user
STATES = ("pending", "explaining", "answered", "parked", "dropped", "handed", "closed")
ANSWER_KINDS = ("option", "note", "explain", "park", "drop", "you_pick", "retract")
MAX_OPTIONS = 5
DUP_RATIO = 0.88
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")
LIMITS = {"title": 80, "question": 300, "label": 40, "then": 120, "means": 400, "step": 140, "cost": 140}
LIMITS |= {"risk": 300, "why_pick": 300, "line": 220, "reason": 300}

# Options used when a claude_decided / tell_someone decision is added without its own options.
DEFAULT_OPTIONS: dict[str, list[dict[str, Any]]] = {
    "claude_decided": [
        {
            "label": "keep",
            "then": "nothing changes; this closes",
            "means": "You agree with what Claude did. It stays as it is.",
            "next_steps": ["Claude records your OK", "Work goes on as it is"],
            "cost": "$0, no time",
            "risk": "None, if the choice was right.",
            "star": True,
            "why_pick": "Claude made this call because it judged it safe; keep it unless you see a problem.",
        },
        {
            "label": "reverse",
            "then": "Claude undoes it and reports what changed",
            "means": "You do not agree. Claude undoes the change.",
            "next_steps": ["Claude undoes the change", "Claude tells you what it changed back"],
            "cost": "$0, some minutes of Claude time",
            "risk": "Work that came after the change may need redoing.",
        },
        {
            "label": "explain",
            "then": "asks Claude to explain; stays in your queue",
            "means": "You need more context before you can say keep or reverse.",
            "next_steps": ["Claude rewrites this decision with more context", "It comes back to your inbox"],
            "cost": "$0",
            "risk": "The decision waits longer.",
        },
    ],
    "tell_someone": [
        {
            "label": "done, I told them",
            "then": "this closes",
            "means": "You already sent the message.",
            "next_steps": ["Claude records that the message went out"],
            "cost": "$0",
            "risk": "None.",
        },
        {
            "label": "draft the message for me",
            "then": "Claude writes it; you send it",
            "means": "Claude writes the message. You read it and send it yourself.",
            "next_steps": ["Claude writes a draft", "You send it"],
            "cost": "$0, 2 minutes of your time",
            "risk": "None.",
            "star": True,
            "why_pick": "A draft is the fastest way to make sure the message goes out.",
        },
        {
            "label": "not needed",
            "then": "this closes; no message",
            "means": "Nobody needs to hear about this.",
            "next_steps": ["Claude closes this item"],
            "cost": "$0",
            "risk": "The person may miss information they need.",
        },
    ],
}


class StoreError(Exception):
    """A store operation failed for a reason the caller can act on."""


class ValidationError(StoreError):
    """A decision did not pass validation; ``errors`` lists each problem."""

    def __init__(self, errors: list[str]) -> None:
        """Keep the list of problems."""
        super().__init__("; ".join(errors))
        self.errors = errors


# ---------------------------------------------------------------- paths + io


def home() -> Path:
    """Return the store root (read from the environment on every call, so tests can redirect it)."""
    env = os.environ.get("HOUSTON_HOME")
    return Path(env).expanduser() if env else Path.home() / ".claude" / "houston"


def user_name() -> str:
    """Return how messages name the person who answers: $HOUSTON_USER_NAME, else config.json "user_name", else "the user"."""
    env = os.environ.get("HOUSTON_USER_NAME", "").strip()
    if env:
        return env
    with contextlib.suppress(OSError, ValueError, AttributeError):
        name = str(json.loads((home() / "config.json").read_text(encoding="utf-8")).get("user_name", "")).strip()
        if name:
            return name
    return "the user"


def cli_command() -> str:
    """How a Claude session should call the CLI: bare ``houston`` inside the plugin (its bin/ is on the Bash
    tool's PATH), else the absolute path of this script."""
    plugin_bin = Path(__file__).resolve().parent.parent / "bin" / "houston"
    if os.environ.get("CLAUDE_PLUGIN_ROOT") and plugin_bin.exists():
        return "houston"
    return f"python3 {Path(__file__).resolve().parent / 'houston.py'}"


def possessive(name: str) -> str:
    """Return "the user's" / "Sam's"."""
    return f"{name}'" if name.endswith("s") else f"{name}'s"


def items_dir() -> Path:
    """Return the directory of decision files."""
    return home() / "items"


def ensure() -> Path:
    """Create the store directories if they are missing; return the root."""
    root = home()
    (root / "items").mkdir(parents=True, exist_ok=True)
    return root


def now_iso() -> str:
    """Return the local time as an ISO string with seconds."""
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def today() -> dt.date:
    """Return today's date (one place, so tests can patch it)."""
    return dt.date.today()


@contextlib.contextmanager
def locked() -> Iterator[None]:
    """Hold the store-wide lock for one read-modify-write."""
    root = ensure()
    with open(root / ".lock", "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def atomic_write_json(path: Path, obj: Any) -> None:
    """Write JSON to a temp file in the same directory, fsync, then rename over the target."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False, indent=1)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def read_json(path: Path, default: Any) -> Any:
    """Read a JSON file, or return ``default`` when it is missing."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def log_event(event: str, **fields: Any) -> None:
    """Append one line to the audit trail."""
    root = ensure()
    row = {"ts": now_iso(), "event": event, **fields}
    with open(root / "events.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def read_events() -> list[dict[str, Any]]:
    """Return every audit-trail row (skipping broken lines)."""
    path = home() / "events.jsonl"
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        with contextlib.suppress(json.JSONDecodeError):
            out.append(json.loads(line))
    return out


# ---------------------------------------------------------------- items


def item_path(did: str) -> Path:
    """Return the file of one decision."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", did):
        raise StoreError(f"bad decision id {did!r}")
    return items_dir() / f"{did}.json"


def load_item(did: str) -> dict[str, Any]:
    """Load one decision or raise ``StoreError`` naming the id."""
    item = read_json(item_path(did), None)
    if item is None:
        raise StoreError(f"no decision with id {did!r} (run `houston list` to see ids)")
    return item


def save_item(item: dict[str, Any]) -> None:
    """Write one decision atomically, stamping ``updated``."""
    item["updated"] = now_iso()
    atomic_write_json(item_path(item["id"]), item)


def all_items(errors: list[str] | None = None) -> list[dict[str, Any]]:
    """Load every decision, oldest first; unreadable files go to ``errors`` instead of raising."""
    out = []
    d = items_dir()
    if not d.exists():
        return []
    for p in sorted(d.glob("*.json")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as e:
            if errors is not None:
                errors.append(f"{p.name}: unreadable ({e})")
    out.sort(key=lambda x: (x.get("created", ""), x.get("seq", 0)))
    return out


def signature() -> tuple[int, float]:
    """Cheap change detector for the inbox's live reload: (file count, newest mtime)."""
    d = items_dir()
    if not d.exists():
        return (0, 0.0)
    mt = [p.stat().st_mtime for p in d.glob("*.json")]
    extra = [p.stat().st_mtime for p in (home() / "threads.json", home() / "staged.json") if p.exists()]
    return (len(mt), max(mt + extra, default=0.0))


def new_id() -> str:
    """Return a short sortable id such as ``261111-a3f9``."""
    return f"{today():%y%m%d}-{uuid.uuid4().hex[:4]}"


def effective_state(item: dict[str, Any], on: dt.date | None = None) -> str:
    """Return the state the inbox shows: a park whose date has passed comes back as pending."""
    state = item.get("state", "pending")
    if state == "parked":
        until = (item.get("park") or {}).get("until_date")
        if until and dt.date.fromisoformat(until) <= (on or today()):
            return "pending"
    return state


def age_days(item: dict[str, Any]) -> int:
    """Return whole days since the decision was first asked."""
    try:
        made = dt.datetime.fromisoformat(item["created"]).date()
    except (KeyError, ValueError):
        return 0
    return max((today() - made).days, 0)


# ---------------------------------------------------------------- threads


def threads() -> dict[str, dict[str, Any]]:
    """Return slug -> thread record."""
    return read_json(home() / "threads.json", {})


def set_thread(
    slug: str, name: str | None = None, goal: str | None = None, state: str | None = None, note: str | None = None
) -> dict[str, Any]:
    """Create or change a thread; only the given fields change."""
    if not SLUG_RE.match(slug):
        raise ValidationError([f"thread slug {slug!r}: use 2-41 chars of a-z, 0-9 and '-', e.g. 'api-v2'"])
    with locked():
        all_t = threads()
        t = all_t.get(slug)
        if t is None:
            if not name:
                raise ValidationError([f"thread {slug!r} is new: give it a display name (--name)"])
            t = {"slug": slug, "name": name, "goal": "", "state": "", "state_note": "", "created": now_iso()}
        for key, val in (("name", name), ("goal", goal), ("state", state), ("state_note", note)):
            if val is not None:
                t[key] = val.strip()
        t["updated"] = now_iso()
        all_t[slug] = t
        atomic_write_json(home() / "threads.json", all_t)
    log_event("thread_set", thread=slug)
    return t


# ---------------------------------------------------------------- validation


def _text(errors: list[str], where: str, val: Any, limit: int, required: bool = True) -> str:
    """Check one text field: present, a string, one line, within its length limit."""
    if val is None or val == "":
        if required:
            errors.append(f"{where}: missing (required)")
        return ""
    if not isinstance(val, str):
        errors.append(f"{where}: must be a string, got {type(val).__name__}")
        return ""
    val = " ".join(val.split())
    if len(val) > limit:
        errors.append(f"{where}: {len(val)} chars, limit {limit}; cut it to the point")
    return val


def _lines(errors: list[str], where: str, val: Any, lo: int, hi: int, limit: int = LIMITS["line"]) -> list[str]:
    """Check a list of short lines (a plain string becomes one line)."""
    if val is None:
        val = []
    if isinstance(val, str):
        val = [val]
    if not isinstance(val, list):
        errors.append(f"{where}: must be a list of strings")
        return []
    out = [_text(errors, f"{where}[{i}]", v, limit) for i, v in enumerate(val)]
    out = [v for v in out if v]
    if not lo <= len(out) <= hi:
        errors.append(f"{where}: {len(out)} lines, need {lo}-{hi}")
    return out


def _options(errors: list[str], kind: str, raw: Any) -> tuple[list[dict[str, Any]], int]:
    """Check the options; return them and the 1-based index of the starred pick."""
    if not raw and kind in DEFAULT_OPTIONS:
        raw = DEFAULT_OPTIONS[kind]
    if not isinstance(raw, list):
        errors.append("options: need a list of 2-5 options, each with its consequence")
        return [], 1
    if len(raw) < 2:
        errors.append(f"options: {len(raw)} given, need 2-5 (a real choice), each with its consequence")
    if len(raw) > MAX_OPTIONS:
        errors.append(f"options: {len(raw)} given, limit {MAX_OPTIONS}; merge or drop the weak ones")
    opts, stars = [], []
    for i, o in enumerate(raw, 1):
        w = f"options[{i}]"
        if not isinstance(o, dict):
            errors.append(f"{w}: must be an object")
            continue
        clean: dict[str, Any] = {
            "label": _text(errors, f"{w}.label", o.get("label"), LIMITS["label"]),
            "then": _text(errors, f"{w}.then (one-line consequence)", o.get("then"), LIMITS["then"]),
            "means": _text(errors, f"{w}.means", o.get("means"), LIMITS["means"]),
            "next_steps": _lines(errors, f"{w}.next_steps", o.get("next_steps"), 1, 5, LIMITS["step"]),
            "cost": _text(errors, f"{w}.cost (money and/or time)", o.get("cost"), LIMITS["cost"]),
            "risk": _text(errors, f"{w}.risk (what can go wrong / what you give up)", o.get("risk"), LIMITS["risk"]),
        }
        if o.get("star"):
            stars.append(i)
            clean["star"] = True
            clean["why_pick"] = _text(
                errors, f"{w}.why_pick (starred option needs it)", o.get("why_pick"), LIMITS["why_pick"]
            )
        elif o.get("why_pick"):
            errors.append(f"{w}.why_pick: only the starred option carries why_pick")
        unknown = set(o) - {"label", "then", "means", "next_steps", "cost", "risk", "star", "why_pick"}
        if unknown:
            errors.append(f"{w}: unknown fields {sorted(unknown)}")
        opts.append(clean)
    if len(stars) != 1:
        errors.append(f'options: star exactly one option ("star": true), found {len(stars)}')
    labels = [o["label"].lower() for o in opts]
    if len(set(labels)) != len(labels):
        errors.append("options: two options share a label")
    return opts, (stars[0] if len(stars) == 1 else 1)


def _git_branch(cwd: str) -> str:
    """Return the git branch of ``cwd`` ('' when unknown)."""
    try:
        r = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def find_transcript(session_id: str) -> str:
    """Find ``~/.claude/projects/*/<session_id>.jsonl`` ('' when absent)."""
    if not session_id or not re.fullmatch(r"[A-Za-z0-9_-]+", session_id):
        return ""
    hits = glob.glob(str(Path.home() / ".claude" / "projects" / "*" / f"{session_id}.jsonl"))
    return hits[0] if hits else ""


def fill_source(src: Any) -> dict[str, Any]:
    """Fill the source fields a session can be told by its environment (session id, cwd, branch, transcript)."""
    s: dict[str, Any] = dict(src) if isinstance(src, dict) else {}
    s.setdefault("session_id", os.environ.get("CLAUDE_CODE_SESSION_ID", ""))
    s.setdefault("cwd", os.getcwd())
    if not s.get("branch") and s.get("cwd"):
        s["branch"] = _git_branch(s["cwd"])
    if not s.get("transcript"):
        s["transcript"] = find_transcript(s.get("session_id", ""))
    s.setdefault("pointer", "")
    s.setdefault("date", today().isoformat())
    return s


def validate(raw: Any, known_threads: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """Return a clean decision body or raise ``ValidationError`` listing every problem at once."""
    if not isinstance(raw, dict):
        raise ValidationError(["input must be one JSON object (see `houston add --example`)"])
    errors: list[str] = []
    known = threads() if known_threads is None else known_threads
    th = raw.get("thread")
    slug, tname = "", None
    if isinstance(th, dict):
        slug, tname = str(th.get("slug", "")), th.get("name")
    elif isinstance(th, str):
        slug = th
    if not SLUG_RE.match(slug):
        errors.append(
            'thread: give {"slug": "a-z0-9-", "name": "Display name"}; run `houston threads` '
            "and reuse a slug when the work is the same"
        )
    elif slug not in known and not tname:
        errors.append(f'thread {slug!r} is new: add "name" (a display name), or reuse a slug from `houston threads`')
    kind = str(raw.get("kind", "")).strip().lower().replace("-", "_")
    if kind not in KINDS:
        errors.append(f"kind: {raw.get('kind')!r} is not one of {', '.join(KINDS)}")
    title = _text(errors, "title", raw.get("title"), LIMITS["title"])
    question = _text(errors, "question", raw.get("question"), LIMITS["question"])
    if isinstance(raw.get("question"), str) and "\n" in raw["question"].strip():
        errors.append("question: one plain sentence, no line breaks; put detail in background")
    opts, pick = _options(errors, kind, raw.get("options"))
    background = _lines(errors, "background (the story so far)", raw.get("background"), 1, 6)
    if "blocks" not in raw:
        errors.append("blocks: missing; list what waits on this decision ([] if nothing)")
    blocks = _lines(errors, "blocks", raw.get("blocks"), 0, 5)
    links = []
    for i, ln in enumerate(raw.get("links") or []):
        if not isinstance(ln, dict) or not ln.get("to") or not ln.get("rel"):
            errors.append(f'links[{i}]: need {{"to": "<decision id>", "rel": "how they relate"}}')
            continue
        links.append({"to": str(ln["to"]), "rel": _text(errors, f"links[{i}].rel", ln["rel"], LIMITS["line"])})
    context = []
    for i, row in enumerate(raw.get("context") or []):
        if not (isinstance(row, (list, tuple)) and len(row) == 2 and all(isinstance(x, str) for x in row)):
            errors.append(f'context[{i}]: need ["USER" or "ASSISTANT", "text"]')
            continue
        context.append([row[0].upper()[:10], row[1][:1000]])
    qa = []
    for i, row in enumerate(raw.get("qa") or []):
        if not isinstance(row, dict) or not row.get("q") or not row.get("a"):
            errors.append(f'qa[{i}]: need {{"q": "...", "a": "..."}}')
            continue
        qa.append({"q": str(row["q"]), "a": str(row["a"]), "source": "canned", "ts": now_iso()})
    urgency = raw.get("urgency", 1)
    if urgency not in (1, 2, 3):
        errors.append("urgency: 1 (normal), 2 (soon) or 3 (blocks work now)")
    src = fill_source(raw.get("source"))
    if not src.get("session_id"):
        errors.append("source.session_id: missing and $CLAUDE_CODE_SESSION_ID is not set")
    unknown = set(raw) - {
        "thread",
        "kind",
        "title",
        "question",
        "options",
        "background",
        "blocks",
        "links",
        "changed_since",
        "context",
        "qa",
        "urgency",
        "source",
    }
    if unknown:
        errors.append(f"unknown fields {sorted(unknown)}")
    if errors:
        raise ValidationError(errors)
    return {
        "thread": slug,
        "thread_name": tname,
        "kind": kind,
        "title": title,
        "question": question,
        "options": opts,
        "pick": pick,
        "background": background,
        "blocks": blocks,
        "changed_since": _lines([], "changed_since", raw.get("changed_since"), 0, 99),
        "links": links,
        "context": context,
        "qa": qa,
        "urgency": urgency,
        "source": src,
    }


def warnings_for(body: dict[str, Any]) -> list[str]:
    """Soft checks: things the writer should fix but that do not block the add."""
    w = []
    if len(body["background"]) < 3:
        w.append("background has fewer than 3 lines; people often ask 'what does this mean?' before deciding")
    if not body["source"].get("transcript"):
        w.append("source.transcript not found; the inbox's context view will fall back to the stored excerpt")
    if not body["question"].endswith("?"):
        w.append("question does not end with '?'; make it the one question the user answers")
    return w


# ---------------------------------------------------------------- add / dedupe / update / close


def _norm(s: str) -> str:
    """Lower-case and strip punctuation, for near-duplicate checks."""
    return " ".join(re.sub(r"[^a-z0-9]+", " ", s.lower()).split())


def similar(a: dict[str, Any], b: dict[str, Any]) -> float:
    """Return the higher of the title and question similarity ratios (0 when their numbers differ: PR #445 != #446)."""
    nums = [set(re.findall(r"\d+", f"{x['title']} {x['question']}")) for x in (a, b)]
    if nums[0] != nums[1]:
        return 0.0
    t = difflib.SequenceMatcher(None, _norm(a["title"]), _norm(b["title"])).ratio()
    q = difflib.SequenceMatcher(None, _norm(a["question"]), _norm(b["question"])).ratio()
    return max(t, q)


def find_duplicate(body: dict[str, Any], items: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return an open decision in the same thread that asks the same thing, if any."""
    best, score = None, 0.0
    for it in items:
        if it["thread"] != body["thread"] or effective_state(it) not in (*OPEN_STATES, "parked"):
            continue
        s = similar(body, it)
        if s >= DUP_RATIO and s > score:
            best, score = it, s
    return best


def _reask(item: dict[str, Any], session_id: str) -> None:
    """Record one more asking of an existing decision."""
    item["times_asked"] = item.get("times_asked", 1) + 1
    item.setdefault("asked_at", []).append(now_iso())
    if session_id and session_id not in item.setdefault("sessions", []):
        item["sessions"].append(session_id)


def add(raw: Any, same_as: str | None = None) -> tuple[str, bool, list[str]]:
    """Add a decision; return (id, created, warnings).

    A near-duplicate re-asks the existing one instead.
    """
    if same_as and raw is None:
        with locked():
            item = load_item(same_as)
            _reask(item, os.environ.get("CLAUDE_CODE_SESSION_ID", ""))
            reopened = _reopen(item)
            save_item(item)
        log_event("re_asked", id=same_as, explicit=True, reopened=reopened, times_asked=item["times_asked"])
        return same_as, False, []
    body = validate(raw)
    warns = warnings_for(body)
    with locked():
        all_t = threads()
        if body["thread"] not in all_t:
            all_t[body["thread"]] = {
                "slug": body["thread"],
                "name": body["thread_name"],
                "goal": "",
                "state": "",
                "state_note": "",
                "created": now_iso(),
                "updated": now_iso(),
            }
            atomic_write_json(home() / "threads.json", all_t)
        target = load_item(same_as) if same_as else find_duplicate(body, all_items())
        if target is not None:
            _reask(target, body["source"].get("session_id", ""))
            reopened = _reopen(target) if same_as else False
            if same_as:
                _apply_content(target, body)
            save_item(target)
            log_event(
                "re_asked",
                id=target["id"],
                explicit=bool(same_as),
                reopened=reopened,
                times_asked=target["times_asked"],
                session=body["source"].get("session_id"),
            )
            return target["id"], False, warns
        did = new_id()
        while item_path(did).exists():
            did = new_id()
        stamp = now_iso()
        item = {
            "schema": SCHEMA,
            "id": did,
            "created": stamp,
            "seq": time.time_ns(),
            "state": "pending",
            "times_asked": 1,
            "asked_at": [stamp],
            "sessions": [body["source"].get("session_id", "")],
            "answers": [],
            "park": None,
            "closed": None,
            "explain": None,
        }
        _apply_content(item, body)
        save_item(item)
    log_event(
        "asked",
        id=did,
        thread=body["thread"],
        kind=body["kind"],
        title=body["title"],
        session=body["source"].get("session_id"),
    )
    return did, True, warns


def _apply_content(item: dict[str, Any], body: dict[str, Any]) -> None:
    """Copy validated content fields onto a stored decision (keeps state, answers, history)."""
    old_qa = [q for q in item.get("qa", []) if q.get("source") != "canned"]
    for key in (
        "thread",
        "kind",
        "title",
        "question",
        "options",
        "pick",
        "background",
        "blocks",
        "changed_since",
        "links",
        "context",
        "urgency",
    ):
        item[key] = body[key]
    item["qa"] = body["qa"] + old_qa
    old_src = item.get("source") or {}
    item["source"] = {**old_src, **{k: v for k, v in body["source"].items() if v}}


def _reopen(item: dict[str, Any]) -> bool:
    """Put a closed or answered decision back to pending (a re-ask of a settled item)."""
    if item.get("state") in OPEN_STATES:
        return False
    item["state"] = "pending"
    item["park"] = None
    return True


def update(did: str, raw: Any) -> dict[str, Any]:
    """Rewrite a decision in place with a full new body; clears an explain request."""
    with locked():
        item = load_item(did)
        merged = {
            k: item.get(k)
            for k in (
                "kind",
                "title",
                "question",
                "options",
                "background",
                "blocks",
                "changed_since",
                "links",
                "context",
                "urgency",
            )
        }
        merged["thread"] = item["thread"]
        merged["source"] = item.get("source")
        merged["qa"] = [{"q": q["q"], "a": q["a"]} for q in item.get("qa", []) if q.get("source") == "canned"]
        if not isinstance(raw, dict):
            raise ValidationError(["update input must be one JSON object with the fields to replace"])
        merged.update(raw)
        body = validate(merged)
        was_explaining = item.get("state") == "explaining"
        _apply_content(item, body)
        if was_explaining:
            item["state"] = "pending"
            item["explain"] = None
            item.setdefault("changed_since", []).insert(0, "Rewritten with more context, as you asked.")
        save_item(item)
    log_event("updated", id=did, cleared_explain=was_explaining)
    return item


def close(did: str, reason: str) -> dict[str, Any]:
    """Close a decision as moot or superseded (by Claude); never deletes it."""
    errors: list[str] = []
    reason = _text(errors, "reason", reason, LIMITS["reason"])
    if errors:
        raise ValidationError(errors)
    with locked():
        item = load_item(did)
        item["state"] = "closed"
        item["closed"] = {
            "by": "claude",
            "reason": reason,
            "ts": now_iso(),
            "session": os.environ.get("CLAUDE_CODE_SESSION_ID", ""),
        }
        save_item(item)
    log_event("closed", id=did, reason=reason)
    return item


def unpark(did: str) -> dict[str, Any]:
    """Bring a parked decision back to pending now."""
    with locked():
        item = load_item(did)
        if item.get("state") == "parked":
            item["state"] = "pending"
            item["park"] = None
            item.setdefault("changed_since", []).insert(0, f"Unparked by you on {today().isoformat()}.")
            save_item(item)
    log_event("unparked", id=did)
    return item


# ---------------------------------------------------------------- answers (the inbox writes, sessions read)


STATE_FOR = {
    "option": "answered",
    "note": "answered",
    "explain": "explaining",
    "park": "parked",
    "drop": "dropped",
    "you_pick": "handed",
}


def answer_text(rec: dict[str, Any]) -> str:
    """Return one line saying what the user answered."""
    k = rec["kind"]
    note = f" · note: {rec['note']}" if rec.get("note") else ""
    if k == "option":
        return f"picked {rec['option']} {rec['label']}{note}"
    if k == "note":
        return f"own answer: {rec['note']}"
    if k == "explain":
        return "explain this better" + (f" · unclear: {rec['note']}" if rec.get("note") else "")
    if k == "park":
        return f"parked until {rec.get('until_event') or rec.get('until_date')}"
    if k == "drop":
        return "drop: not needed any more"
    if k == "you_pick":
        return f"you (Claude) pick · your star was {rec['option']} {rec['label']}"
    return f"retracted: {user_name()} undid the last answer; the decision is open again"


def submit_batch(staged: dict[str, dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """Apply every staged choice as one batch; return (batch id, undo snapshots)."""
    batch = uuid.uuid4().hex[:8]
    ts = now_iso()
    snaps = []
    with locked():
        for did, st in staged.items():
            item = load_item(did)
            snaps.append(
                {"id": did, "state": item.get("state"), "park": item.get("park"), "explain": item.get("explain")}
            )
            k = st["kind"]
            if k not in STATE_FOR:
                raise StoreError(f"unknown staged kind {k!r}")
            rec = {
                "batch": batch,
                "ts": ts,
                "kind": k,
                "option": st.get("option"),
                "label": st.get("label"),
                "note": st.get("note") or None,
                "until_date": st.get("until_date"),
                "until_event": st.get("until_event"),
                "delivered_at": None,
                "delivered_to": None,
            }
            item.setdefault("answers", []).append(rec)
            item["state"] = STATE_FOR[k]
            item["park"] = (
                {"until_date": st.get("until_date"), "until_event": st.get("until_event")} if k == "park" else None
            )
            item["explain"] = {"unclear": st.get("note") or "", "ts": ts} if k == "explain" else None
            save_item(item)
    log_event("batch_submitted", batch=batch, ids=list(staged), kinds=[s["kind"] for s in staged.values()])
    return batch, snaps


def undo_batch(batch: str, snaps: list[dict[str, Any]]) -> None:
    """Undo a submitted batch: drop its undelivered answers, retract delivered ones, restore states."""
    with locked():
        for snap in snaps:
            item = load_item(snap["id"])
            keep = []
            retract = False
            for rec in item.get("answers", []):
                if rec.get("batch") != batch:
                    keep.append(rec)
                elif rec.get("delivered_at"):
                    keep.append(rec)
                    retract = True
            if retract:
                keep.append(
                    {
                        "batch": batch,
                        "ts": now_iso(),
                        "kind": "retract",
                        "option": None,
                        "label": None,
                        "note": None,
                        "until_date": None,
                        "until_event": None,
                        "delivered_at": None,
                        "delivered_to": None,
                    }
                )
            item["answers"] = keep
            item["state"], item["park"], item["explain"] = snap["state"], snap["park"], snap["explain"]
            save_item(item)
    log_event("batch_undone", batch=batch, ids=[s["id"] for s in snaps])


def pending_answers(session: str | None = None, thread: str | None = None) -> list[dict[str, Any]]:
    """Return undelivered answers, each as {id, thread, title, question, answer: record, text}."""
    out = []
    for item in all_items():
        if session and session not in item.get("sessions", []) and item.get("source", {}).get("session_id") != session:
            continue
        if thread and item.get("thread") != thread:
            continue
        for i, rec in enumerate(item.get("answers", [])):
            if rec.get("delivered_at"):
                continue
            row = {
                "id": item["id"],
                "index": i,
                "thread": item["thread"],
                "title": item["title"],
                "question": item["question"],
                "kind": item["kind"],
                "answer": rec,
                "text": answer_text(rec),
            }
            if rec["kind"] in ("option", "you_pick") and rec.get("option"):
                opt = item["options"][rec["option"] - 1]
                row["option"] = {"label": opt["label"], "means": opt["means"], "next_steps": opt["next_steps"]}
            out.append(row)
    return out


def mark_delivered(rows: list[dict[str, Any]], to: str) -> int:
    """Record that the given answer rows reached a session."""
    ts = now_iso()
    n = 0
    with locked():
        by_id: dict[str, list[int]] = {}
        for r in rows:
            by_id.setdefault(r["id"], []).append(r["index"])
        for did, idxs in by_id.items():
            item = load_item(did)
            for i in idxs:
                rec = item["answers"][i]
                if not rec.get("delivered_at"):
                    rec["delivered_at"], rec["delivered_to"] = ts, to
                    n += 1
            save_item(item)
    log_event("delivered", to=to, ids=sorted(by_id), n=n)
    return n


def add_qa(did: str, q: str, a: str) -> None:
    """Keep one asked question and its answer on the decision."""
    with locked():
        item = load_item(did)
        item.setdefault("qa", []).append({"q": q, "a": a, "source": "asked", "ts": now_iso()})
        save_item(item)
    log_event("question_asked", id=did)


# ---------------------------------------------------------------- staged picks (owned by the inbox)


def load_staged() -> dict[str, Any]:
    """Return {"staged": {id: choice}, "drafts": {id: {note_text, attach_note, unclear}}}."""
    data = read_json(home() / "staged.json", {})
    return {"staged": data.get("staged", {}), "drafts": data.get("drafts", {})}


def save_staged(staged: dict[str, Any], drafts: dict[str, Any]) -> None:
    """Persist staged picks and typed drafts so they survive an inbox restart."""
    with locked():
        atomic_write_json(home() / "staged.json", {"staged": staged, "drafts": drafts, "ts": now_iso()})


# ---------------------------------------------------------------- source transcript


SKIP_PREFIX = (
    "<",
    "Base directory for this skill",
    "Another Claude session sent a message",
    "[Request interrupted",
    "Caveat:",
    "This session is being continued",
)


def _turn(o: dict[str, Any]) -> tuple[str, str, str] | None:
    """Turn one transcript line into (who, text, ts) when it is human or assistant prose."""
    t = o.get("type")
    msg = o.get("message") or {}
    content = msg.get("content")
    if t == "user":
        origin = o.get("origin")
        if isinstance(origin, dict) and origin.get("kind") not in (None, "human"):
            return None
        if o.get("isMeta") or o.get("isSidechain"):
            return None
        if isinstance(content, list):
            if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
                return None
            content = " ".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
        if not isinstance(content, str) or not content.strip() or content.lstrip().startswith(SKIP_PREFIX):
            return None
        return ("USER", content.strip(), o.get("timestamp", ""))
    if t == "assistant" and not o.get("isSidechain") and isinstance(content, list):
        text = "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
        if text.strip():
            return ("ASSISTANT", text.strip(), o.get("timestamp", ""))
    return None


def transcript_turns(path: str, until_ts: str = "", before: int = 8, after: int = 2) -> list[list[str]] | None:
    """Stream a transcript and return the prose turns around ``until_ts`` (None when the file is missing).

    Files can be tens of MB, so lines are read one at a time and only user/assistant lines are parsed.
    """
    p = Path(path).expanduser() if path else None
    if p is None or not p.is_file():
        return None
    cutoff = None
    if until_ts:
        with contextlib.suppress(ValueError):
            cutoff = dt.datetime.fromisoformat(until_ts.replace("Z", "+00:00"))
    head: deque[list[str]] = deque(maxlen=before)
    tail: list[list[str]] = []
    with open(p, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if '"user"' not in line and '"assistant"' not in line:  # cheap prefilter before json.loads
                continue
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            turn = _turn(o)
            if turn is None:
                continue
            who, text, ts = turn
            row = [who, text if len(text) <= 1500 else text[:1500] + " …"]
            late = False
            if cutoff and ts:
                with contextlib.suppress(ValueError):
                    late = dt.datetime.fromisoformat(ts.replace("Z", "+00:00")) > cutoff
            if late:
                tail.append(row)
                if len(tail) >= after:
                    break
            else:
                head.append(row)
    return list(head) + tail


def context_for(item: dict[str, Any]) -> tuple[str, list[list[str]]]:
    """Return (where it came from, turns): transcript first, then the stored excerpt, then nothing."""
    src = item.get("source") or {}
    turns = transcript_turns(src.get("transcript", ""), (item.get("asked_at") or [item.get("created", "")])[0])
    if turns:
        return "transcript", turns
    if item.get("context"):
        return "excerpt", item["context"]
    return "none", []


# ---------------------------------------------------------------- doctor


def doctor() -> dict[str, Any]:
    """Check store health: counts, unreadable or invalid files, dangling links, orphan temp files."""
    errors: list[str] = []
    items = all_items(errors)
    known = threads()
    ids = {i.get("id") for i in items}
    counts: dict[str, int] = {}
    for it in items:
        st = effective_state(it)
        counts[st] = counts.get(st, 0) + 1
        where = it.get("id", "?")
        for key in ("id", "thread", "kind", "title", "question", "options", "pick", "state", "source", "created"):
            if key not in it:
                errors.append(f"{where}: missing field {key!r}")
        if it.get("state") not in STATES:
            errors.append(f"{where}: unknown state {it.get('state')!r}")
        if it.get("thread") not in known:
            errors.append(f"{where}: thread {it.get('thread')!r} not in threads.json")
        for ln in it.get("links", []):
            if ln.get("to") not in ids:
                errors.append(f"{where}: link to unknown decision {ln.get('to')!r}")
    if items_dir().exists():
        for p in items_dir().glob("*.json"):
            with contextlib.suppress(OSError, json.JSONDecodeError):
                if json.loads(p.read_text(encoding="utf-8")).get("id") != p.stem:
                    errors.append(f"{p.name}: file name does not match its id")
    orphans = [p.name for p in items_dir().glob(".*.tmp")] if items_dir().exists() else []
    staged = load_staged()["staged"]
    for did in staged:
        if did not in ids:
            errors.append(f"staged.json: staged pick for unknown decision {did!r}")
    undelivered = len(pending_answers())
    return {
        "home": str(home()),
        "items": len(items),
        "states": counts,
        "threads": len(known),
        "staged": len(staged),
        "undelivered_answers": undelivered,
        "orphan_temp_files": orphans,
        "errors": errors,
        "events": len(read_events()),
    }
