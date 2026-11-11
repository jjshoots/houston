"""Plugin packaging: manifest, marketplace and hooks JSON follow the Claude Code plugin spec; every path exists."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import store
from conftest import ROOT

KEBAB = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
RESERVED_PREFIXES = ("claude-", "anthropic-", "anthropics-", "cc-plugin-")
HOOK_EVENTS = {"SessionStart", "UserPromptSubmit", "Stop"}


def load(rel: str) -> dict:
    """Read one JSON file from the repo root."""
    return json.loads((ROOT / rel).read_text())


def test_plugin_manifest() -> None:
    """plugin.json: kebab-case name, not reserved, description/version/author present, URLs well formed."""
    m = load(".claude-plugin/plugin.json")
    assert KEBAB.match(m["name"]) and not m["name"].startswith(RESERVED_PREFIXES)
    assert m["name"] not in ("claude", "anthropic", "claude-code")
    assert m["description"] and m["version"] and m["author"]["name"]
    for key in ("homepage", "repository"):
        assert m[key].startswith("https://")
    assert "license" not in m, "no license chosen yet"


def test_marketplace() -> None:
    """marketplace.json: name, owner.name, plugins[] with a ./ source that resolves to this plugin."""
    mk = load(".claude-plugin/marketplace.json")
    assert re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*$", mk["name"]) and ".." not in mk["name"]
    assert mk["owner"]["name"]
    names = load(".claude-plugin/plugin.json")["name"]
    [entry] = mk["plugins"]
    assert entry["name"] == names and entry["source"].startswith("./")
    assert (ROOT / entry["source"] / ".claude-plugin" / "plugin.json").exists()


def test_hooks_json_wiring_and_paths() -> None:
    """hooks.json has the 'hooks' wrapper, the three events, and commands that point at existing scripts."""
    h = load("hooks/hooks.json")
    assert set(h["hooks"]) == HOOK_EVENTS
    want = {"SessionStart": "hook_context.py", "UserPromptSubmit": "hook_context.py", "Stop": "hook_stop.py"}
    for event, groups in h["hooks"].items():
        for g in groups:
            for hk in g["hooks"]:
                assert hk["type"] == "command" and 0 < hk["timeout"] <= 30
                paths = re.findall(r"\$\{CLAUDE_PLUGIN_ROOT\}/([^\"\s]+)", hk["command"])
                assert paths and paths[0].endswith(want[event])
                assert all((ROOT / p).is_file() for p in paths)
    assert "matcher" not in h["hooks"]["Stop"][0], "Stop ignores matchers"


def test_skill_and_bin() -> None:
    """The skill has frontmatter with its name; bin/houston is executable and runs the CLI."""
    text = (ROOT / "skills/houston/SKILL.md").read_text()
    assert text.startswith("---\nname: houston\ndescription: ")
    exe = ROOT / "bin" / "houston"
    assert os.access(exe, os.X_OK)
    r = subprocess.run([str(exe), "list"], capture_output=True, text=True, timeout=30, check=False)
    assert r.returncode == 0 and "No decisions match." in r.stdout


def test_user_name_and_cli_command(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Messages say 'the user' by default, a configured name when set; the CLI is bare 'houston' inside the plugin."""
    assert store.user_name() == "the user"
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.json").write_text('{"user_name": "Sam"}')
    assert store.user_name() == "Sam" and store.possessive("Sam") == "Sam's"
    monkeypatch.setenv("HOUSTON_USER_NAME", "Ana")
    assert store.user_name() == "Ana"
    assert store.cli_command().startswith("python3 ") and store.cli_command().endswith("houston.py")
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(ROOT))
    assert store.cli_command() == "houston"


@pytest.mark.skipif(shutil.which("claude") is None, reason="claude CLI not installed")
def test_claude_plugin_validate() -> None:
    """`claude plugin validate` passes on the repo (plugin + marketplace)."""
    r = subprocess.run(["claude", "plugin", "validate", str(ROOT)], capture_output=True, text=True, timeout=120,
                       check=False, cwd=str(ROOT), env={**os.environ, "PYTHONPATH": ""})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Validation passed" in r.stdout


def test_no_python_outside_stdlib_in_hooks() -> None:
    """Hooks and the store import only the standard library (they run on plain python3)."""
    for name in ("hook_context.py", "hook_stop.py", "store.py"):
        src = (ROOT / "scripts" / name).read_text()
        mods = re.findall(r"^\s*(?:import|from)\s+([a-zA-Z_][\w]*)", src, re.M)
        for m in mods:
            assert m in sys.stdlib_module_names or m in ("store", "__future__"), (name, m)
