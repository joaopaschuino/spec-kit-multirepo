"""Tests for governance-note.sh — the member-side AGENTS.md governance section.

The script is the persistent channel that keeps free-form edits (which bypass
the implement command) subject to the workspace constitution layers, so its
idempotency and content preservation are behavioral contracts.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from conftest import PACKAGE_ROOT

SCRIPT = PACKAGE_ROOT / "governance-note.sh"
START = "<!-- SPECKIT-MULTI-REPO:GOVERNANCE START -->"
END = "<!-- SPECKIT-MULTI-REPO:GOVERNANCE END -->"


def run_note(project: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), str(project)],
        capture_output=True,
        text=True,
    )


def test_creates_agents_md_when_missing(tmp_path):
    result = run_note(tmp_path)
    assert result.returncode == 0, result.stderr
    text = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert START in text and END in text
    assert "## Multi-repo workspace governance" in text
    assert "most restrictive rule wins" in text


def test_appends_to_existing_agents_md(tmp_path):
    (tmp_path / "AGENTS.md").write_text(
        "# My project\n\nExisting notes.\n", encoding="utf-8"
    )
    result = run_note(tmp_path)
    assert result.returncode == 0, result.stderr
    text = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert text.startswith("# My project\n")
    assert "Existing notes." in text
    assert text.index("# My project") < text.index(START)


def test_rerun_replaces_section_without_duplicating(tmp_path):
    (tmp_path / "AGENTS.md").write_text("before\n", encoding="utf-8")
    assert run_note(tmp_path).returncode == 0
    # Tamper with the section body, then re-run: the section is reset in
    # place, exactly once, and surrounding content survives.
    agents = tmp_path / "AGENTS.md"
    agents.write_text(
        agents.read_text(encoding="utf-8").replace("layers 1–3", "tampered"),
        encoding="utf-8",
    )
    assert run_note(tmp_path).returncode == 0
    text = agents.read_text(encoding="utf-8")
    assert text.count(START) == 1
    assert text.count(END) == 1
    assert "tampered" not in text
    assert "before" in text


def test_preserves_content_after_the_section(tmp_path):
    (tmp_path / "AGENTS.md").write_text(f"{START}\nold\n{END}\nafter\n", encoding="utf-8")
    assert run_note(tmp_path).returncode == 0
    text = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert text.endswith("after\n")
    assert "old" not in text


def test_missing_project_dir_fails(tmp_path):
    result = run_note(tmp_path / "ghost")
    assert result.returncode != 0
