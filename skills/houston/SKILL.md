---
name: houston
description: Log every decision that waits on the user in the cross-session decision inbox, and read the user's answers back. Use whenever you would ask the user to choose, ask for a go-ahead or a spend, defer something to "later", point to open items in a file, need the user to tell or ask another person, or decide something yourself that the user did not approve. Also use at session start and before any status brief, to collect answers. The user answers in the inbox by running `houston` in a terminal.
---

# houston

The user works with many sessions, often in many worktrees. A decision asked
only in chat gets lost: footer lists and "more in file X" are rarely
answered. So every decision goes into ONE store (`~/.claude/houston/`), the
user answers them in batch in the inbox, and the answers come back to the
session that asked.

The `houston` command is on your Bash PATH (the plugin's `bin/`). Your session
id is `$CLAUDE_CODE_SESSION_ID`; `houston add` fills it, the cwd, the branch
and the transcript path for you.

## When to log a decision

Log one each time you would:

- ask the user to choose between options, or ask "which do you want";
- ask for a go-ahead, or for money or quota (`spend`);
- defer something to "later", or point to open items in a file or footer;
- need the user to tell or ask another person (`tell_someone`);
- decide something yourself that the user did not approve
  (`claude_decided`: options default to keep / reverse / explain; give
  `background` and `blocks`).

A decision asked in chat must ALSO be in the store. End the chat reply with
"N decisions are in the inbox: run `houston`", not a footer list.

## How to log

1. `houston threads`. Reuse the slug if the work is the same thread.
2. Write the JSON (`houston add --example` prints a valid one) and run
   `houston add < /tmp/decision.json`. Fix every error it prints and run
   again. If it says "already in the inbox as <id>", do not add it again.
3. To ask again on purpose, `houston add --same-as <id>`. Keep the thread's
   header current with `houston thread set <slug> --goal … --state … --note …`.

Fields: `thread` {slug, name}, `kind` (spend / go_ahead / ruling / scope /
design / tell_someone / claude_decided), `title` (≤80 chars), `question`
(one plain, self-contained sentence), `options` (2-5: `label`, `then` = the
one-line consequence, `means`, `next_steps`, `cost`, `risk`; exactly one has
`star: true` + `why_pick`), `background` (the story so far, 3-5 lines),
`blocks` (what waits on it). Optional: `links` [{to, rel}],
`changed_since`, `context` [[USER|ASSISTANT, text]], `qa` [{q, a}],
`urgency` 1-3, `source.pointer` (PR, file, ticket).

Write for a reader who comes back cold, days later:

- Plain words, short sentences, active voice. Give each internal code name
  a 3-6 word gloss.
- Every option says what happens, what it costs, and what it risks.
- Star the option you would pick, and say why in one line.
- Max 5 options. No option that only means "ask me later": the inbox has
  park, drop, "you pick" and "explain this better" built in.

## Read the answers

At session start and before any status brief:
`houston answers --session $CLAUDE_CODE_SESSION_ID --mark-delivered`. Act on
each:

- **picked N / own answer**: do it; a note on the pick is part of the answer.
- **you pick**: decide yourself; the star is the default. Report what you did.
- **explain this better**: rewrite with `houston update <id> < /tmp/new.json`
  (richer background, clearer options, explain what the user said is
  unclear). It goes back to the inbox.
- **park**: do nothing; it comes back on its date or when the user unparks it.
- **drop**: stop that work. **retracted**: the user undid the answer; it is
  open again.

When work makes a decision moot, `houston close <id> --reason "<why>"`. Never
leave it open, and never delete a file in the store.
