"""End-to-end test: install the step + workflow via the real CLI and run.

Covers the full chain the extension promises: ``specify workflow step add
--dev``, ``specify workflow add --dev``, ``workflow run`` with gate verdicts
pre-approved via inputs, headless member dispatch through a fake agent
executable (SPECKIT_INTEGRATION_CLAUDE_EXECUTABLE), dependency-wave ordering,
feature-axis propagation into the agent process, and ``workflow resume``
after a member failure.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from conftest import PACKAGE_ROOT, make_member, write_workspace

pytest.importorskip("specify_cli")

RUN_ID = "mre2e1"


@pytest.fixture()
def fake_agent(tmp_path, monkeypatch):
    """A shell-script stand-in for the claude CLI.

    Appends one record per invocation to $FAKE_AGENT_LOG and exits 1 when a
    ``fail-agent`` sentinel file exists in the working directory.
    """
    log = tmp_path / "agent-log.jsonl"
    script = tmp_path / "fake-agent.sh"
    script.write_text(
        "#!/bin/sh\n"
        "log=\"${FAKE_AGENT_LOG:?}\"\n"
        "{\n"
        "  printf 'cwd=%s\\n' \"$(pwd)\"\n"
        "  printf 'args=%s\\n' \"$*\"\n"
        "  printf 'feature=%s\\n' \"${SPECIFY_FEATURE_DIRECTORY:-}\"\n"
        "  printf 'no_persist=%s\\n' \"${SPECIFY_FEATURE_NO_PERSIST:-}\"\n"
        "  printf -- '---\\n'\n"
        "} >> \"$log\"\n"
        "if [ -f fail-agent ]; then\n"
        "  exit 1\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("SPECKIT_INTEGRATION_CLAUDE_EXECUTABLE", str(script))
    monkeypatch.setenv("FAKE_AGENT_LOG", str(log))
    return log


@pytest.fixture()
def central(tmp_path, fake_agent, monkeypatch):
    from conftest import make_feature

    root = write_workspace(
        tmp_path,
        members=[
            {"id": "s3", "path": "repo-s3"},
            {"id": "lambda", "path": "repo-lambda", "depends_on": ["s3"]},
        ],
    )
    make_member(tmp_path, "s3", with_specify=True)
    lambda_repo = make_member(tmp_path, "lambda", with_specify=True)
    # Members use the claude integration key; the executable is overridden
    # per test via the environment.
    for member_id in ("s3", "lambda"):
        member = tmp_path / f"repo-{member_id}"
        (member / ".specify" / "integration.json").write_text(
            json.dumps({"integration": "claude"}), encoding="utf-8"
        )
    feature = make_feature(
        root,
        task_files={"s3": "- [ ] s3 task\n", "lambda": "- [ ] lambda task\n"},
    )
    monkeypatch.setenv("SPECKIT_WORKFLOW_RUN_ID", RUN_ID)
    monkeypatch.chdir(root)
    return root, feature, lambda_repo


def invoke_specify(argv):
    from typer.testing import CliRunner

    from specify_cli import app

    return CliRunner().invoke(app, argv, catch_exceptions=True)


def read_agent_log(log: Path) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    for block in log.read_text(encoding="utf-8").split("---\n"):
        if not block.strip():
            continue
        records.append(dict(line.split("=", 1) for line in block.strip().splitlines()))
    return records


def read_run_state(central: Path) -> dict:
    state_path = central / ".specify" / "workflows" / "runs" / RUN_ID / "state.json"
    return json.loads(state_path.read_text(encoding="utf-8"))


def test_install_run_and_resume(central):
    root, feature, lambda_repo = central

    # -- install the components through the real CLI -----------------------
    result = invoke_specify(
        ["workflow", "step", "add", "multi-repo-implement", "--dev", str(PACKAGE_ROOT / "step" / "multi-repo-implement")]
    )
    assert result.exit_code == 0, result.output

    result = invoke_specify(
        ["workflow", "add", str(PACKAGE_ROOT / "workflow" / "multi-repo"), "--dev"]
    )
    assert result.exit_code == 0, result.output

    # -- first run: lambda fails (sentinel), run fails ---------------------
    (lambda_repo / "fail-agent").write_text("", encoding="utf-8")
    result = invoke_specify(
        [
            "workflow", "run", "multi-repo",
            "-i", "confirm=approve",
            "-i", "review=approve",
            "-i", f"feature={feature.relative_to(root).as_posix()}",
        ]
    )
    assert result.exit_code == 1, result.output  # failed run exits non-zero

    log = read_agent_log(Path(os.environ["FAKE_AGENT_LOG"]))
    assert [r["args"].split(" ")[-1] for r in log] == ["tasks/s3.md", "tasks/lambda.md"]
    s3_call = log[0]
    assert s3_call["cwd"] == str(root / "repo-s3")
    # claude is a skills integration: the extension command's native
    # invocation form is the hyphenated /speckit-multi-repo-implement.
    assert "/speckit-multi-repo-implement" in s3_call["args"]
    assert "tasks/s3.md" in s3_call["args"]
    assert s3_call["feature"] == str(feature)
    assert s3_call["no_persist"] == "1"

    state = read_run_state(root)
    assert state["status"] == "failed"
    implement = state["step_results"]["implement"]
    assert implement["status"] == "failed"
    per_repo = {r["repo"]: r for r in implement["output"]["results"]}
    assert per_repo["s3"]["status"] == "completed"
    assert per_repo["lambda"]["status"] == "failed"
    assert implement["output"]["waves"] == [["s3"], ["lambda"]]

    # -- resume after fixing the member: run completes ---------------------
    (lambda_repo / "fail-agent").unlink()
    result = invoke_specify(["workflow", "resume", RUN_ID])
    assert result.exit_code == 0, result.output

    state = read_run_state(root)
    assert state["status"] == "completed"
    implement = state["step_results"]["implement"]
    per_repo = {r["repo"]: r for r in implement["output"]["results"]}
    assert all(r["status"] == "completed" for r in per_repo.values())
    # The resumed dispatch re-ran s3 too (idempotent no-op expectation) and
    # then lambda.
    log = read_agent_log(Path(os.environ["FAKE_AGENT_LOG"]))
    assert [r["args"].split(" ")[-1] for r in log] == [
        "tasks/s3.md", "tasks/lambda.md",  # first run
        "tasks/s3.md", "tasks/lambda.md",  # resume re-executed the step
    ]


def test_extension_installs_and_registers_commands(tmp_path, fake_agent, monkeypatch):
    """The extension manifest validates and its commands reach the member.

    Members are claude (skills) projects, so the extension commands register
    as .claude/skills/speckit-multi-repo-<name> — the exact invocation the
    orchestrator step dispatches. The member is bootstrapped with the real
    ``specify init`` because command registration needs the integration's
    installed artifacts.
    """
    member = tmp_path / "repo-lambda"
    member.mkdir()
    monkeypatch.chdir(member)
    result = invoke_specify(
        ["init", "--here", "--integration", "claude", "--script", "sh", "--ignore-agent-tools", "--force"]
    )
    assert result.exit_code == 0, result.output

    result = invoke_specify(
        ["extension", "add", str(PACKAGE_ROOT), "--dev"]
    )
    assert result.exit_code == 0, result.output

    assert (member / ".specify" / "extensions" / "multi-repo" / "extension.yml").is_file()
    skills = member / ".claude" / "skills"
    assert (skills / "speckit-multi-repo-implement").exists()
    assert (skills / "speckit-multi-repo-tasks").exists()
    hooks = (member / ".specify" / "extensions.yml").read_text(encoding="utf-8")
    assert "after_tasks" in hooks
    assert "speckit.multi-repo.tasks" in hooks
