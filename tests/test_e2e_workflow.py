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
    ``fail-agent`` sentinel file exists in the working directory. With a
    ``fix-on-feedback`` sentinel, a retry dispatch (verification feedback
    file among the args) creates ``fixed``; ``hang-agent`` makes it start a
    background child that writes ``late-write`` after 4s, then hang.
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
        "case \"$*\" in *feedback.md*)\n"
        "  if [ -f fix-on-feedback ]; then : > fixed; fi ;;\n"
        "esac\n"
        "if [ -f hang-agent ]; then\n"
        "  ( sleep 4; : > late-write ) &\n"
        "  sleep 30\n"
        "fi\n"
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
            {"id": "s3", "path": "repo-s3", "constitution": "constitutions/s3.md"},
            {"id": "lambda", "path": "repo-lambda", "depends_on": ["s3"]},
        ],
    )
    constitutions = root / "constitutions"
    constitutions.mkdir()
    (constitutions / "s3.md").write_text("# s3 type rules\n", encoding="utf-8")
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


def install_components():
    """Install the step and the workflows through the real CLI."""
    result = invoke_specify(
        ["workflow", "step", "add", "multi-repo-implement", "--dev", str(PACKAGE_ROOT / "step" / "multi-repo-implement")]
    )
    assert result.exit_code == 0, result.output
    for name in ("multi-repo", "multi-repo-check", "multi-repo-bootstrap"):
        result = invoke_specify(
            ["workflow", "add", str(PACKAGE_ROOT / "workflow" / name), "--dev"]
        )
        assert result.exit_code == 0, result.output


def test_install_run_and_resume(central):
    root, feature, lambda_repo = central

    # -- install the components through the real CLI -----------------------
    install_components()

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
    assert len(log) == 2
    # s3 carries its governance bundle (all constitution layers assembled by
    # the orchestrator); lambda gets its own bundle too (uniform protocol).
    assert "tasks/s3.md" in log[0]["args"]
    assert "governance/s3.md" in log[0]["args"]
    assert "governance/lambda.md" in log[1]["args"]
    assert "# s3 type rules" in (feature / "governance" / "s3.md").read_text(encoding="utf-8")
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
    assert len(log) == 4
    for s3_call, lambda_call in ((log[0], log[1]), (log[2], log[3])):
        assert "tasks/s3.md" in s3_call["args"]
        assert "governance/s3.md" in s3_call["args"]
        assert "governance/lambda.md" in lambda_call["args"]


def test_multi_repo_check_reports_without_dispatch(central):
    """The check workflow reports readiness and waves, dispatching nothing."""
    root, feature, _lambda_repo = central
    install_components()

    result = invoke_specify(
        ["workflow", "run", "multi-repo-check", "-i", f"feature={feature.relative_to(root).as_posix()}"]
    )
    assert result.exit_code == 0, result.output

    # No agent was executed.
    assert not Path(os.environ["FAKE_AGENT_LOG"]).exists()

    state = read_run_state(root)
    assert state["status"] == "completed"
    check = state["step_results"]["check"]
    assert check["status"] == "completed"
    output = check["output"]
    assert output["dry_run"] is True
    assert output["waves"] == [["s3"], ["lambda"]]
    assert output["results"] == []
    members = {m["repo"]: m for m in output["members"]}
    assert members["s3"]["status"] == "ready"
    assert members["s3"]["participant"] is True
    assert members["s3"]["integration"] == "claude"
    assert members["s3"]["constitution"].endswith("constitutions/s3.md")
    assert members["lambda"]["status"] == "ready"


def test_missing_skip_dispatches_ready_members(central):
    """missing=skip through the real workflow: uncloned member recorded, rest dispatched."""
    root, feature, lambda_repo = central
    install_components()

    # Make the lambda member locally absent: hide its Spec Kit scaffolding.
    (lambda_repo / ".specify").rename(lambda_repo / ".specify-bak")

    result = invoke_specify(
        [
            "workflow", "run", "multi-repo",
            "-i", "confirm=approve",
            "-i", "review=approve",
            "-i", f"feature={feature.relative_to(root).as_posix()}",
            "-i", "missing=skip",
        ]
    )
    assert result.exit_code == 0, result.output  # run completes with skips

    log = read_agent_log(Path(os.environ["FAKE_AGENT_LOG"]))
    # s3 dispatched (its tasks file and governance bundle are among the
    # args), lambda skipped as missing.
    tasks_args = [[t for t in r["args"].split(" ") if t.startswith("tasks/")] for r in log]
    assert tasks_args == [["tasks/s3.md"]]

    state = read_run_state(root)
    assert state["status"] == "completed"
    implement = state["step_results"]["implement"]
    assert implement["status"] == "completed"
    output = implement["output"]
    assert output["waves"] == [["s3"]]
    assert output["missing"] == ["lambda"]
    assert output["failed"] == []
    per_repo = {r["repo"]: r for r in output["results"]}
    assert per_repo["s3"]["status"] == "completed"
    assert per_repo["lambda"]["status"] == "missing"
    assert "not a Spec Kit project" in per_repo["lambda"]["reason"]


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
    assert (skills / "speckit-multi-repo-help").exists()
    assert (skills / "speckit-multi-repo-domain").exists()
    hooks = (member / ".specify" / "extensions.yml").read_text(encoding="utf-8")
    assert "after_tasks" in hooks
    assert "speckit.multi-repo.tasks" in hooks


def run_multi_repo(root, feature, *extra):
    argv = [
        "workflow", "run", "multi-repo",
        "-i", "confirm=approve",
        "-i", "review=approve",
        "-i", f"feature={feature.relative_to(root).as_posix()}",
    ]
    for item in extra:
        argv.extend(["-i", item])
    return invoke_specify(argv)


def test_verification_retry_status_and_review_gate(central):
    """Failed checks re-dispatch with feedback; Status column and gate follow."""
    root, feature, lambda_repo = central
    install_components()
    (feature / "tasks.md").write_text(
        "# Orchestration page\n\n"
        "| Member | Task file | Depends on | Status |\n"
        "|--------|-----------|------------|--------|\n"
        "| s3 | tasks/s3.md | — | pending |\n"
        "| lambda | tasks/lambda.md | s3 | pending |\n",
        encoding="utf-8",
    )
    (feature / "tasks" / "s3.md").write_text(
        "- [ ] s3 task\n\n## Verification\n\n- lint: `true` → exit 0\n", encoding="utf-8"
    )
    (feature / "tasks" / "lambda.md").write_text(
        "- [ ] lambda task\n\n## Verification\n\n"
        '- test: `test -f fixed && test "$SPECIFY_FEATURE_DIRECTORY" = "'
        + str(feature)
        + '"` → exit 0\n',
        encoding="utf-8",
    )
    (lambda_repo / "fix-on-feedback").write_text("", encoding="utf-8")

    result = run_multi_repo(root, feature, "verify_retries=1")
    assert result.exit_code == 0, result.output

    log = read_agent_log(Path(os.environ["FAKE_AGENT_LOG"]))
    assert [r["cwd"] for r in log] == [
        str(root / "repo-s3"),
        str(lambda_repo),
        str(lambda_repo),
    ]
    assert "lambda.feedback.md" in log[2]["args"]
    assert "lambda.feedback.md" not in log[1]["args"]

    state = read_run_state(root)
    assert state["status"] == "completed"
    output = state["step_results"]["implement"]["output"]
    per_repo = {r["repo"]: r for r in output["results"]}
    assert per_repo["s3"]["verification"]["status"] == "verified"
    assert per_repo["lambda"]["verification"]["status"] == "verified"
    assert per_repo["lambda"]["attempts"] == 2
    assert output["verification"] == {"s3": "verified", "lambda": "verified"}

    page = (feature / "tasks.md").read_text(encoding="utf-8")
    assert "| s3 | tasks/s3.md | — | verified |" in page
    assert "| lambda | tasks/lambda.md | s3 | verified |" in page

    review = state["step_results"]["review"]["output"]
    assert "verified: s3, lambda" in review["message"]
    assert review["show_file"] == str(feature / "verification" / "report.md")
    assert "| lambda | completed | verified | 2 | — |" in (
        feature / "verification" / "report.md"
    ).read_text(encoding="utf-8")


def test_verification_failure_fails_run_without_retries(central):
    root, feature, _lambda_repo = central
    install_components()
    (feature / "tasks" / "s3.md").write_text(
        "- [ ] s3 task\n\n## Verification\n\n- test: `exit 1`\n", encoding="utf-8"
    )

    result = run_multi_repo(root, feature)
    assert result.exit_code == 1, result.output

    assert len(read_agent_log(Path(os.environ["FAKE_AGENT_LOG"]))) == 1  # default: no retry
    implement = read_run_state(root)["step_results"]["implement"]
    assert implement["status"] == "failed"
    s3 = {r["repo"]: r for r in implement["output"]["results"]}["s3"]
    assert s3["status"] == "failed"
    assert s3["verification"]["status"] == "failed"
    assert (feature / "verification" / "s3.log").is_file()


def test_sequential_dispatch_timeout_kills_hung_agent(central):
    import time

    root, feature, _lambda_repo = central
    install_components()
    (root / "repo-s3" / "hang-agent").write_text("", encoding="utf-8")

    started = time.monotonic()
    result = run_multi_repo(root, feature, "dispatch_timeout=2")
    assert time.monotonic() - started < 25
    assert result.exit_code == 1, result.output

    implement = read_run_state(root)["step_results"]["implement"]
    s3 = {r["repo"]: r for r in implement["output"]["results"]}["s3"]
    assert s3["status"] == "failed"
    assert s3["timed_out"] is True
    assert "lambda" not in {r["repo"] for r in implement["output"]["results"]}
    # The agent's descendants were killed with it: no write after the deadline.
    time.sleep(max(0.0, started + 5 - time.monotonic()))
    assert not (root / "repo-s3" / "late-write").exists()


def test_contract_check_member_runs_after_producer(central):
    root, feature, lambda_repo = central
    install_components()
    manifest_path = root / ".specify" / "workspace.yml"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["members"].append(
        {
            "id": "contract-check",
            "path": "repo-lambda",
            "tasks_file": "tasks/contract-check.md",
            "depends_on": ["s3"],
        }
    )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    (feature / "contracts").mkdir()
    (feature / "contracts" / "events.schema.json").write_text("{}", encoding="utf-8")
    (feature / "tasks" / "contract-check.md").write_text(
        "# Contract check\n\n## Verification\n\n"
        '- schema: `test -f "$SPECIFY_FEATURE_DIRECTORY/contracts/events.schema.json"`\n'
        "- producer-ran: `grep -q repo-s3 \"$FAKE_AGENT_LOG\"`\n",
        encoding="utf-8",
    )

    result = run_multi_repo(root, feature)
    assert result.exit_code == 0, result.output

    output = read_run_state(root)["step_results"]["implement"]["output"]
    assert output["waves"] == [["s3"], ["contract-check", "lambda"]]
    check = {r["repo"]: r for r in output["results"]}["contract-check"]
    assert check["mode"] == "verify-only"
    assert check["verification"]["status"] == "verified"
    # Only the two implementation members dispatched an agent.
    assert len(read_agent_log(Path(os.environ["FAKE_AGENT_LOG"]))) == 2


def test_monorepo_members_are_subdirectories(tmp_path, fake_agent, monkeypatch):
    """Mono-repo: the root is central, members are subdirectories of it."""
    from conftest import make_feature

    root = tmp_path / "mono"
    members = {"api": "services/api", "worker": "services/worker", "web": "apps/web"}
    write_workspace(
        root,
        members=[
            {"id": "api", "path": members["api"]},
            {"id": "worker", "path": members["worker"], "depends_on": ["api"]},
            {"id": "web", "path": members["web"], "depends_on": ["api"]},
        ],
    )
    for rel in members.values():
        specify = root / rel / ".specify"
        specify.mkdir(parents=True)
        (specify / "integration.json").write_text(
            json.dumps({"integration": "claude"}), encoding="utf-8"
        )
    feature = make_feature(
        root,
        task_files={
            member_id: f"- [ ] {member_id} task\n\n## Verification\n\n"
            f'- cwd: `test "$PWD" = "{root / rel}"` → exit 0\n'
            for member_id, rel in members.items()
        },
    )
    monkeypatch.setenv("SPECKIT_WORKFLOW_RUN_ID", RUN_ID)
    monkeypatch.chdir(root)
    install_components()

    result = run_multi_repo(root, feature)
    assert result.exit_code == 0, result.output

    output = read_run_state(root)["step_results"]["implement"]["output"]
    assert output["waves"] == [["api"], ["web", "worker"]]
    per_repo = {r["repo"]: r for r in output["results"]}
    assert {r["status"] for r in per_repo.values()} == {"completed"}
    assert {r["verification"]["status"] for r in per_repo.values()} == {"verified"}
    log = read_agent_log(Path(os.environ["FAKE_AGENT_LOG"]))
    cwds = [r["cwd"] for r in log]
    assert cwds[0] == str(root / members["api"])
    assert sorted(cwds[1:]) == sorted(str(root / members[m]) for m in ("web", "worker"))
    assert all(r["feature"] == str(feature) for r in log)


def test_multi_repo_bootstrap_clones_missing_members(central, tmp_path):
    """The bootstrap workflow clones an absent member from its manifest url."""
    import subprocess

    root, _feature, _lambda_repo = central
    remote = tmp_path / "remotes" / "glue"
    remote.mkdir(parents=True)
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com"]
    subprocess.run([*git, "init", "-q"], cwd=remote, check=True)
    (remote / "README.md").write_text("# glue\n", encoding="utf-8")
    subprocess.run([*git, "add", "."], cwd=remote, check=True)
    subprocess.run([*git, "commit", "-q", "-m", "init"], cwd=remote, check=True)
    write_workspace(
        root,
        members=[
            {"id": "s3", "path": "repo-s3", "constitution": "constitutions/s3.md"},
            {"id": "lambda", "path": "repo-lambda", "depends_on": ["s3"]},
            {"id": "glue", "path": "members/glue", "url": remote.as_uri()},
        ],
    )
    install_components()

    result = invoke_specify(["workflow", "run", "multi-repo-bootstrap"])
    assert result.exit_code == 0, result.output

    assert (root / "members" / "glue" / "README.md").is_file()
    state = read_run_state(root)
    output = state["step_results"]["bootstrap"]["output"]
    assert output["cloned"] == ["glue"]
    assert output["present"] == ["s3", "lambda"]
    assert output["gitignore"] == ["/members/glue/"]
    assert "/members/glue/" in (root / ".gitignore").read_text(encoding="utf-8")
    assert not Path(os.environ["FAKE_AGENT_LOG"]).exists()  # nothing dispatched
