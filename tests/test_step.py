"""Tests for the multi-repo-implement workflow step."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from conftest import (
    FakeIntegration,
    make_feature,
    make_member,
    write_workspace,
)

S3 = "repo-s3"
LAMBDA = "repo-lambda"
GLUE = "repo-glue"


@pytest.fixture()
def workspace_setup(tmp_path):
    """Central repo + s3/lambda members (lambda depends on s3) + feature."""
    central = write_workspace(
        tmp_path,
        members=[
            {"id": "s3", "path": "repo-s3"},
            {"id": "lambda", "path": "repo-lambda", "depends_on": ["s3"]},
            {"id": "glue", "path": "repo-glue"},  # not part of the feature
        ],
    )
    members = {
        "s3": make_member(tmp_path, "s3"),
        "lambda": make_member(tmp_path, "lambda"),
        "glue": make_member(tmp_path, "glue"),
    }
    feature = make_feature(central, task_files={"s3": "", "lambda": ""})
    return central, members, feature


def patch_integration(monkeypatch, calls, fail_repos=None):
    """Point get_integration at a FakeIntegration.

    The step imports get_integration lazily inside its methods, so patching
    the source module attribute covers every call site. FakeIntegration keys
    failure by the member directory basename (repo-<id>).
    """
    import specify_cli.integrations as integrations_module

    fake = FakeIntegration(calls, fail_repos)
    monkeypatch.setattr(integrations_module, "get_integration", lambda key: fake)


def make_context(step_module, project_root, inputs=None):
    return step_module.StepContext(project_root=str(project_root), inputs=inputs or {})


def test_execute_dispatches_in_waves(workspace_setup, step_module, monkeypatch, dispatch_calls):
    central, members, feature = workspace_setup
    monkeypatch.delenv("SPECIFY_FEATURE_DIRECTORY", raising=False)
    (central / ".specify" / "feature.json").write_text(
        json.dumps({"feature_directory": "specs/001-demo"}), encoding="utf-8"
    )
    patch_integration(monkeypatch, dispatch_calls)

    result = step_module.MultiRepoImplementStep().execute(
        {"id": "implement", "type": "multi-repo-implement"}, make_context(step_module, central)
    )

    assert result.status == step_module.StepStatus.COMPLETED
    assert result.output["workspace"] == "platform"
    assert result.output["feature"] == str(feature)
    assert result.output["waves"] == [["s3"], ["lambda"]]
    assert result.output["failed"] == []
    assert [c["member"] for c in dispatch_calls] == [S3, LAMBDA]  # dependency order
    for call in dispatch_calls:
        assert call["command"] == "speckit.multi-repo.implement"
        assert call["project_root"] == str(members[call["member"].removeprefix("repo-")])
        assert call["args"].startswith("tasks/")
        assert call["stream"] is True  # sequential waves stream live output
    per_repo = {r["repo"]: r for r in result.output["results"]}
    assert per_repo["s3"]["wave"] == 0
    assert per_repo["lambda"]["wave"] == 1
    assert "glue" not in per_repo  # auto-detection: no tasks file in feature


def test_execute_feature_from_config_and_env(workspace_setup, step_module, monkeypatch, dispatch_calls):
    central, _members, _feature = workspace_setup
    patch_integration(monkeypatch, dispatch_calls)

    # Explicit 'feature' setting wins over the environment.
    monkeypatch.setenv("SPECIFY_FEATURE_DIRECTORY", "/nonexistent/from-env")
    result = step_module.MultiRepoImplementStep().execute(
        {"id": "i", "type": "multi-repo-implement", "feature": "specs/001-demo"},
        make_context(step_module, central),
    )
    assert result.status == step_module.StepStatus.COMPLETED

    # Environment fallback with an absolute central feature path.
    monkeypatch.setenv("SPECIFY_FEATURE_DIRECTORY", str(central / "specs" / "001-demo"))
    result = step_module.MultiRepoImplementStep().execute(
        {"id": "i", "type": "multi-repo-implement"}, make_context(step_module, central)
    )
    assert result.status == step_module.StepStatus.COMPLETED


def test_execute_exports_feature_env_and_restores(
    workspace_setup, step_module, monkeypatch, dispatch_calls
):
    central, _members, _feature = workspace_setup
    monkeypatch.setenv("SPECIFY_FEATURE_DIRECTORY", "/pre-existing")
    monkeypatch.delenv("SPECIFY_FEATURE_NO_PERSIST", raising=False)
    captured = {}

    class EnvCapture(FakeIntegration):
        def dispatch_command(self, command_name, args="", *, project_root=None, **kwargs):
            captured[str(project_root)] = (
                os.environ.get("SPECIFY_FEATURE_DIRECTORY"),
                os.environ.get("SPECIFY_FEATURE_NO_PERSIST"),
            )
            return super().dispatch_command(
                command_name, args, project_root=project_root, **kwargs
            )

    import specify_cli.integrations as integrations_module

    monkeypatch.setattr(
        integrations_module, "get_integration", lambda key: EnvCapture(dispatch_calls)
    )

    step_module.MultiRepoImplementStep().execute(
        {"id": "i", "type": "multi-repo-implement", "feature": "specs/001-demo"},
        make_context(step_module, central),
    )

    for env in captured.values():
        assert env[0] == str(central / "specs" / "001-demo")
        assert env[1] == "1"  # no-persist by default
    # Original values restored after the step.
    assert os.environ["SPECIFY_FEATURE_DIRECTORY"] == "/pre-existing"
    assert "SPECIFY_FEATURE_NO_PERSIST" not in os.environ


def test_execute_persist_feature_removes_no_persist(
    workspace_setup, step_module, monkeypatch, dispatch_calls
):
    central, _members, _feature = workspace_setup
    monkeypatch.delenv("SPECIFY_FEATURE_DIRECTORY", raising=False)
    monkeypatch.setenv("SPECIFY_FEATURE_NO_PERSIST", "1")
    captured = {}

    class EnvCapture(FakeIntegration):
        def dispatch_command(self, command_name, args="", *, project_root=None, **kwargs):
            captured[str(project_root)] = os.environ.get("SPECIFY_FEATURE_NO_PERSIST")
            return super().dispatch_command(
                command_name, args, project_root=project_root, **kwargs
            )

    import specify_cli.integrations as integrations_module

    monkeypatch.setattr(
        integrations_module, "get_integration", lambda key: EnvCapture(dispatch_calls)
    )

    step_module.MultiRepoImplementStep().execute(
        {
            "id": "i",
            "type": "multi-repo-implement",
            "feature": "specs/001-demo",
            "persist_feature": True,
        },
        make_context(step_module, central),
    )

    assert set(captured.values()) == {None}  # removed for dispatch...
    assert os.environ.get("SPECIFY_FEATURE_NO_PERSIST") == "1"  # ...and restored after


def test_execute_repos_subset(workspace_setup, step_module, monkeypatch, dispatch_calls):
    central, _members, _feature = workspace_setup
    patch_integration(monkeypatch, dispatch_calls)

    result = step_module.MultiRepoImplementStep().execute(
        {"id": "i", "type": "multi-repo-implement", "feature": "specs/001-demo", "repos": ["lambda"]},
        make_context(step_module, central),
    )

    assert result.status == step_module.StepStatus.COMPLETED
    assert [c["member"] for c in dispatch_calls] == [LAMBDA]
    assert result.output["waves"] == [["lambda"]]  # s3 dropped from subgraph


def test_execute_skip_completed(workspace_setup, step_module, monkeypatch, dispatch_calls):
    central, _members, feature = workspace_setup
    (feature / "tasks" / "lambda.md").write_text(
        "- [x] done task\n- [X] also done\n", encoding="utf-8"
    )
    patch_integration(monkeypatch, dispatch_calls)

    result = step_module.MultiRepoImplementStep().execute(
        {"id": "i", "type": "multi-repo-implement", "feature": "specs/001-demo"},
        make_context(step_module, central),
    )

    assert result.status == step_module.StepStatus.COMPLETED
    assert [c["member"] for c in dispatch_calls] == [S3]  # lambda skipped
    per_repo = {r["repo"]: r for r in result.output["results"]}
    assert per_repo["lambda"]["status"] == "skipped"


def test_execute_stop_on_failure(workspace_setup, step_module, monkeypatch, dispatch_calls):
    central, _members, _feature = workspace_setup
    patch_integration(monkeypatch, dispatch_calls, fail_repos={S3: 1})

    result = step_module.MultiRepoImplementStep().execute(
        {"id": "i", "type": "multi-repo-implement", "feature": "specs/001-demo"},
        make_context(step_module, central),
    )

    assert result.status == step_module.StepStatus.FAILED
    assert [c["member"] for c in dispatch_calls] == [S3]  # lambda never dispatched
    assert result.output["failed"] == ["s3"]
    assert result.output["stopped_after_wave"] == 0
    assert "1 member dispatch(es) failed: s3 (exit 1)" in (result.error or "")


def test_execute_continue_on_failure(workspace_setup, step_module, monkeypatch, dispatch_calls):
    central, _members, _feature = workspace_setup
    patch_integration(monkeypatch, dispatch_calls, fail_repos={S3: 1})

    result = step_module.MultiRepoImplementStep().execute(
        {
            "id": "i",
            "type": "multi-repo-implement",
            "feature": "specs/001-demo",
            "stop_on_failure": False,
        },
        make_context(step_module, central),
    )

    assert result.status == step_module.StepStatus.FAILED
    assert [c["member"] for c in dispatch_calls] == [S3, LAMBDA]  # lambda still ran
    assert result.output["stopped_after_wave"] is None
    per_repo = {r["repo"]: r for r in result.output["results"]}
    assert per_repo["lambda"]["status"] == "completed"
    assert per_repo["s3"]["status"] == "failed"
    assert per_repo["s3"]["error"] == "boom"


def test_execute_parallel_wave_uses_captured_dispatch(
    workspace_setup, step_module, monkeypatch, dispatch_calls
):
    central, _members, _feature = workspace_setup
    patch_integration(monkeypatch, dispatch_calls)

    result = step_module.MultiRepoImplementStep().execute(
        {
            "id": "i",
            "type": "multi-repo-implement",
            "feature": "specs/001-demo",
            "repos": ["s3", "glue"],  # independent => same wave
            "max_concurrency": 2,
        },
        make_context(step_module, central),
    )

    assert result.status == step_module.StepStatus.COMPLETED
    assert result.output["waves"] == [["glue", "s3"]]
    assert sorted(c["member"] for c in dispatch_calls) == sorted([GLUE, S3])
    assert all(c["stream"] is False for c in dispatch_calls)  # captured under timeout


def test_execute_invalid_member_blocks_all(workspace_setup, step_module, monkeypatch, dispatch_calls):
    central, members, _feature = workspace_setup
    # Break the s3 member: no integration configured anywhere.
    (members["s3"] / ".specify" / "integration.json").unlink()
    patch_integration(monkeypatch, dispatch_calls)

    result = step_module.MultiRepoImplementStep().execute(
        {"id": "i", "type": "multi-repo-implement", "feature": "specs/001-demo"},
        make_context(step_module, central),
    )

    assert result.status == step_module.StepStatus.FAILED
    assert dispatch_calls == []  # validated before any dispatch
    assert "Member validation failed" in (result.error or "")
    assert "s3" in (result.error or "")


def test_execute_unknown_repo_in_config(workspace_setup, step_module, monkeypatch, dispatch_calls):
    central, _members, _feature = workspace_setup
    patch_integration(monkeypatch, dispatch_calls)

    result = step_module.MultiRepoImplementStep().execute(
        {"id": "i", "type": "multi-repo-implement", "feature": "specs/001-demo", "repos": ["ghost"]},
        make_context(step_module, central),
    )

    assert result.status == step_module.StepStatus.FAILED
    assert dispatch_calls == []
    assert "No workspace members participate" in (result.error or "")


def test_execute_no_feature(workspace_setup, step_module, monkeypatch, dispatch_calls):
    central, _members, _feature = workspace_setup
    monkeypatch.delenv("SPECIFY_FEATURE_DIRECTORY", raising=False)
    patch_integration(monkeypatch, dispatch_calls)

    result = step_module.MultiRepoImplementStep().execute(
        {"id": "i", "type": "multi-repo-implement"}, make_context(step_module, central)
    )

    assert result.status == step_module.StepStatus.FAILED
    assert "No feature directory" in (result.error or "")


def test_execute_no_participants(workspace_setup, step_module, monkeypatch, dispatch_calls):
    central, _members, _feature = workspace_setup
    empty_feature = make_feature(central, slug="002-empty", task_files={})
    patch_integration(monkeypatch, dispatch_calls)

    result = step_module.MultiRepoImplementStep().execute(
        {"id": "i", "type": "multi-repo-implement", "feature": str(empty_feature)},
        make_context(step_module, central),
    )

    assert result.status == step_module.StepStatus.FAILED
    assert "No workspace members participate" in (result.error or "")


def test_execute_missing_workspace(tmp_path, step_module, monkeypatch, dispatch_calls):
    patch_integration(monkeypatch, dispatch_calls)
    result = step_module.MultiRepoImplementStep().execute(
        {"id": "i", "type": "multi-repo-implement"}, make_context(step_module, tmp_path)
    )
    assert result.status == step_module.StepStatus.FAILED
    assert "workspace manifest" in (result.error or "").lower()


def test_execute_member_constitution_in_args(tmp_path, step_module, monkeypatch, dispatch_calls):
    """Every dispatch receives its governance bundle as the second token."""
    central = write_workspace(
        tmp_path,
        members=[
            {"id": "s3", "path": "repo-s3", "constitution": "constitutions/s3.md"},
            {"id": "lambda", "path": "repo-lambda", "depends_on": ["s3"]},
        ],
    )
    make_member(tmp_path, "s3")
    make_member(tmp_path, "lambda")
    make_feature(central, task_files={"s3": "", "lambda": ""})
    constitution = central / "constitutions" / "s3.md"
    constitution.parent.mkdir()
    constitution.write_text("# s3 type rules\n", encoding="utf-8")
    patch_integration(monkeypatch, dispatch_calls)

    result = step_module.MultiRepoImplementStep().execute(
        {"id": "i", "type": "multi-repo-implement", "feature": "specs/001-demo"},
        make_context(step_module, central),
    )

    assert result.status == step_module.StepStatus.COMPLETED
    governance = central / "specs" / "001-demo" / "governance"
    by_args = {c["member"]: c for c in dispatch_calls}
    assert by_args[S3]["args"] == f"tasks/s3.md {governance / 's3.md'}"
    # The bundle protocol is uniform: dispatched even without a configured
    # constitution, so the agent never has to guess which layers exist.
    assert by_args[LAMBDA]["args"] == f"tasks/lambda.md {governance / 'lambda.md'}"
    assert "# s3 type rules" in (governance / "s3.md").read_text(encoding="utf-8")
    lambda_text = (governance / "lambda.md").read_text(encoding="utf-8")
    assert "Layer 2" in lambda_text
    assert "Not present" in lambda_text
    per_repo = {r["repo"]: r for r in result.output["results"]}
    assert per_repo["s3"]["constitution"] == str(constitution)
    assert per_repo["s3"]["governance"] == str(governance / "s3.md")
    assert per_repo["lambda"]["constitution"] is None
    assert per_repo["lambda"]["governance"] == str(governance / "lambda.md")


def test_execute_parallel_members_get_their_own_constitution(
    tmp_path, step_module, monkeypatch, dispatch_calls
):
    """Concurrent waves each receive their own bundle (args, not shared env)."""
    central = write_workspace(
        tmp_path,
        members=[
            {"id": "s3", "path": "repo-s3", "constitution": "constitutions/s3.md"},
            {"id": "glue", "path": "repo-glue", "constitution": "constitutions/glue.md"},
        ],
    )
    make_member(tmp_path, "s3")
    make_member(tmp_path, "glue")
    make_feature(central, task_files={"s3": "", "glue": ""})
    constitutions = central / "constitutions"
    constitutions.mkdir()
    (constitutions / "s3.md").write_text("# s3\n", encoding="utf-8")
    (constitutions / "glue.md").write_text("# glue\n", encoding="utf-8")
    patch_integration(monkeypatch, dispatch_calls)

    result = step_module.MultiRepoImplementStep().execute(
        {
            "id": "i",
            "type": "multi-repo-implement",
            "feature": "specs/001-demo",
            "max_concurrency": 2,
        },
        make_context(step_module, central),
    )

    assert result.status == step_module.StepStatus.COMPLETED
    by_args = {c["member"]: c for c in dispatch_calls}
    assert by_args[S3]["args"].endswith("governance/s3.md")
    assert by_args[GLUE]["args"].endswith("governance/glue.md")
    governance = central / "specs" / "001-demo" / "governance"
    assert "# s3" in (governance / "s3.md").read_text(encoding="utf-8")
    assert "# glue" in (governance / "glue.md").read_text(encoding="utf-8")


def test_execute_governance_bundle_assembles_all_layers(
    tmp_path, step_module, monkeypatch, dispatch_calls
):
    """Workspace, member/type, and local layers all land in the bundle."""
    central = write_workspace(
        tmp_path,
        members=[{"id": "s3", "path": "repo-s3", "constitution": "constitutions/s3.md"}],
    )
    member = make_member(tmp_path, "s3")
    make_feature(central, task_files={"s3": ""})
    memory = central / ".specify" / "memory"
    memory.mkdir(parents=True)
    (memory / "constitution.md").write_text("# workspace rules\n", encoding="utf-8")
    constitution = central / "constitutions" / "s3.md"
    constitution.parent.mkdir()
    constitution.write_text("# s3 type rules\n", encoding="utf-8")
    local_memory = member / ".specify" / "memory"
    local_memory.mkdir(parents=True)
    (local_memory / "constitution.md").write_text("# s3 local rules\n", encoding="utf-8")
    patch_integration(monkeypatch, dispatch_calls)

    result = step_module.MultiRepoImplementStep().execute(
        {"id": "i", "type": "multi-repo-implement", "feature": "specs/001-demo"},
        make_context(step_module, central),
    )

    assert result.status == step_module.StepStatus.COMPLETED
    bundle = central / "specs" / "001-demo" / "governance" / "s3.md"
    text = bundle.read_text(encoding="utf-8")
    assert "## Layer 1 — Workspace constitution" in text
    assert "# workspace rules" in text
    assert "## Layer 2 — Member/type constitution" in text
    assert "# s3 type rules" in text
    assert "## Layer 3 — Local constitution" in text
    assert "# s3 local rules" in text
    # The dispatch received the bundle, not the raw constitution path.
    assert dispatch_calls[0]["args"] == f"tasks/s3.md {bundle}"


def test_execute_governance_bundle_without_any_layers(
    tmp_path, step_module, monkeypatch, dispatch_calls
):
    """No constitutions anywhere: the bundle is still written and dispatched."""
    central = write_workspace(tmp_path, members=[{"id": "s3", "path": "repo-s3"}])
    make_member(tmp_path, "s3")
    make_feature(central, task_files={"s3": ""})
    patch_integration(monkeypatch, dispatch_calls)

    result = step_module.MultiRepoImplementStep().execute(
        {"id": "i", "type": "multi-repo-implement", "feature": "specs/001-demo"},
        make_context(step_module, central),
    )

    assert result.status == step_module.StepStatus.COMPLETED
    bundle = central / "specs" / "001-demo" / "governance" / "s3.md"
    assert bundle.read_text(encoding="utf-8").count("Not present") == 3
    assert dispatch_calls[0]["args"].endswith("governance/s3.md")


def test_execute_template_constitution_is_reported_absent(
    tmp_path, step_module, monkeypatch, dispatch_calls
):
    """A local layer still holding the specify init template defines nothing."""
    central = write_workspace(tmp_path, members=[{"id": "s3", "path": "repo-s3"}])
    member = make_member(tmp_path, "s3")
    make_feature(central, task_files={"s3": ""})
    local_memory = member / ".specify" / "memory"
    local_memory.mkdir(parents=True)
    (local_memory / "constitution.md").write_text(
        "# [PROJECT_NAME] Constitution\n[PRINCIPLE_1_DESCRIPTION]\n",
        encoding="utf-8",
    )
    patch_integration(monkeypatch, dispatch_calls)

    result = step_module.MultiRepoImplementStep().execute(
        {"id": "i", "type": "multi-repo-implement", "feature": "specs/001-demo"},
        make_context(step_module, central),
    )

    assert result.status == step_module.StepStatus.COMPLETED
    bundle = central / "specs" / "001-demo" / "governance" / "s3.md"
    text = bundle.read_text(encoding="utf-8")
    assert "## Layer 3" in text
    assert "Not present" in text.split("## Layer 3")[1]
    assert "[PRINCIPLE_1_DESCRIPTION]" not in text  # template noise never dispatched


def test_execute_unreadable_constitution_blocks_dispatch(
    tmp_path, step_module, monkeypatch, dispatch_calls
):
    """A constitution that exists but cannot be read fails before any dispatch."""
    central = write_workspace(
        tmp_path,
        members=[{"id": "s3", "path": "repo-s3", "constitution": "constitutions/s3.md"}],
    )
    make_member(tmp_path, "s3")
    make_feature(central, task_files={"s3": ""})
    constitution = central / "constitutions" / "s3.md"
    constitution.parent.mkdir()
    constitution.write_bytes(b"# s3 type rules \xff\xfe\n")  # invalid UTF-8
    patch_integration(monkeypatch, dispatch_calls)

    result = step_module.MultiRepoImplementStep().execute(
        {"id": "i", "type": "multi-repo-implement", "feature": "specs/001-demo"},
        make_context(step_module, central),
    )

    assert result.status == step_module.StepStatus.FAILED
    assert dispatch_calls == []  # fail fast before any dispatch
    assert "Cannot read the member constitution" in (result.error or "")
    assert "s3" in (result.error or "")


def test_execute_unwritable_bundle_fails_that_member(
    tmp_path, step_module, monkeypatch, dispatch_calls
):
    """A bundle that cannot be written fails the member instead of raising."""
    central = write_workspace(
        tmp_path,
        members=[{"id": "s3", "path": "repo-s3", "constitution": "constitutions/s3.md"}],
    )
    make_member(tmp_path, "s3")
    feature = make_feature(central, task_files={"s3": ""})
    (central / "constitutions").mkdir()
    (central / "constitutions" / "s3.md").write_text("# s3\n", encoding="utf-8")
    feature.chmod(0o555)  # governance/ cannot be created inside the feature dir
    patch_integration(monkeypatch, dispatch_calls)
    try:
        result = step_module.MultiRepoImplementStep().execute(
            {"id": "i", "type": "multi-repo-implement", "feature": "specs/001-demo"},
            make_context(step_module, central),
        )

        assert result.status == step_module.StepStatus.FAILED
        assert dispatch_calls == []  # the agent never ran without its bundle
        per_repo = {r["repo"]: r for r in result.output["results"]}
        assert per_repo["s3"]["status"] == "failed"
        assert "governance bundle failed" in (per_repo["s3"]["error"] or "")
    finally:
        feature.chmod(0o755)


def test_execute_missing_constitution_blocks_dispatch(
    tmp_path, step_module, monkeypatch, dispatch_calls
):
    central = write_workspace(
        tmp_path,
        members=[{"id": "s3", "path": "repo-s3", "constitution": "constitutions/s3.md"}],
    )
    make_member(tmp_path, "s3")
    make_feature(central, task_files={"s3": ""})
    # No constitutions/ directory: the configured file does not exist.
    patch_integration(monkeypatch, dispatch_calls)

    result = step_module.MultiRepoImplementStep().execute(
        {"id": "i", "type": "multi-repo-implement", "feature": "specs/001-demo"},
        make_context(step_module, central),
    )

    assert result.status == step_module.StepStatus.FAILED
    assert dispatch_calls == []  # fail fast before any dispatch
    assert "constitution file not found" in (result.error or "")
    assert "s3" in (result.error or "")


def test_execute_dry_run_reports_without_dispatch(
    tmp_path, step_module, monkeypatch, dispatch_calls
):
    """Dry run classifies every declared member and dispatches nothing."""
    central = write_workspace(
        tmp_path,
        members=[
            {"id": "s3", "path": "repo-s3"},
            {"id": "lambda", "path": "repo-lambda", "depends_on": ["s3"]},
        ],
    )
    make_member(tmp_path, "s3")  # ready
    # lambda: directory absent entirely (never cloned)
    make_feature(central, task_files={"s3": "", "lambda": ""})
    patch_integration(monkeypatch, dispatch_calls)

    result = step_module.MultiRepoImplementStep().execute(
        {
            "id": "i",
            "type": "multi-repo-implement",
            "feature": "specs/001-demo",
            "dry_run": True,
        },
        make_context(step_module, central),
    )

    assert result.status == step_module.StepStatus.COMPLETED
    assert dispatch_calls == []  # nothing dispatched
    assert result.output["dry_run"] is True
    assert result.output["results"] == []
    assert result.output["failed"] == []
    assert result.output["missing"] == ["lambda"]
    assert result.output["waves"] == [["s3"]]  # only ready participants

    members = {m["repo"]: m for m in result.output["members"]}
    assert members["s3"]["status"] == "ready"
    assert members["s3"]["participant"] is True
    assert members["lambda"]["status"] == "not-cloned"
    assert members["lambda"]["participant"] is True
    assert members["lambda"]["reason"] == (
        f"member path not found: {(tmp_path / 'repo-lambda').resolve()}"
    )


def test_execute_dry_run_without_feature_reports_whole_workspace(
    tmp_path, step_module, monkeypatch, dispatch_calls
):
    """A feature-less dry run is the workspace-level readiness report."""
    central = write_workspace(
        tmp_path,
        members=[
            {"id": "s3", "path": "repo-s3"},
            {"id": "glue", "path": "repo-glue", "constitution": "constitutions/glue.md"},
        ],
    )
    make_member(tmp_path, "s3")
    make_member(tmp_path, "glue")
    patch_integration(monkeypatch, dispatch_calls)
    monkeypatch.delenv("SPECIFY_FEATURE_DIRECTORY", raising=False)

    result = step_module.MultiRepoImplementStep().execute(
        {"id": "i", "type": "multi-repo-implement", "dry_run": True},
        make_context(step_module, central),
    )

    assert result.status == step_module.StepStatus.COMPLETED
    assert dispatch_calls == []
    members = {m["repo"]: m for m in result.output["members"]}
    assert members["s3"]["status"] == "ready"
    assert members["glue"]["status"] == "constitution-missing"
    assert result.output["waves"] == [["s3"]]  # glue not dispatchable


def test_execute_missing_skip_dispatches_ready_and_reports_missing(
    tmp_path, step_module, monkeypatch, dispatch_calls
):
    central = write_workspace(
        tmp_path,
        members=[
            {"id": "s3", "path": "repo-s3"},
            {"id": "lambda", "path": "repo-lambda", "depends_on": ["s3"]},
        ],
    )
    make_member(tmp_path, "s3")
    # lambda: cloned but bare — no .specify/ (never initialized)
    (tmp_path / "repo-lambda").mkdir()
    make_feature(central, task_files={"s3": "", "lambda": ""})
    patch_integration(monkeypatch, dispatch_calls)

    result = step_module.MultiRepoImplementStep().execute(
        {
            "id": "i",
            "type": "multi-repo-implement",
            "feature": "specs/001-demo",
            "missing": "skip",
        },
        make_context(step_module, central),
    )

    assert result.status == step_module.StepStatus.COMPLETED
    assert [c["member"] for c in dispatch_calls] == [S3]
    assert result.output["waves"] == [["s3"]]
    assert result.output["missing"] == ["lambda"]
    assert result.output["failed"] == []
    per_repo = {r["repo"]: r for r in result.output["results"]}
    assert per_repo["s3"]["status"] == "completed"
    assert per_repo["lambda"]["status"] == "missing"
    assert "not a Spec Kit project" in per_repo["lambda"]["reason"]


def test_execute_missing_skip_still_fails_on_config_errors(
    tmp_path, step_module, monkeypatch, dispatch_calls
):
    """skip covers missing clones, not workspace configuration mistakes."""
    central = write_workspace(
        tmp_path,
        members=[
            {"id": "s3", "path": "repo-s3"},
            {"id": "lambda", "path": "repo-lambda"},
        ],
    )
    s3 = make_member(tmp_path, "s3")
    (s3 / ".specify" / "integration.json").unlink()  # config error
    # lambda not cloned at all
    make_feature(central, task_files={"s3": ""})
    patch_integration(monkeypatch, dispatch_calls)

    result = step_module.MultiRepoImplementStep().execute(
        {
            "id": "i",
            "type": "multi-repo-implement",
            "feature": "specs/001-demo",
            "missing": "skip",
        },
        make_context(step_module, central),
    )

    assert result.status == step_module.StepStatus.FAILED
    assert dispatch_calls == []
    assert "Member validation failed" in (result.error or "")
    assert "no integration configured" in (result.error or "")


def test_execute_uncloned_participant_fails_by_default(
    tmp_path, step_module, monkeypatch, dispatch_calls
):
    central = write_workspace(
        tmp_path,
        members=[{"id": "lambda", "path": "repo-lambda"}],
    )
    make_feature(central, task_files={"lambda": ""})
    # repo-lambda never cloned
    patch_integration(monkeypatch, dispatch_calls)

    result = step_module.MultiRepoImplementStep().execute(
        {"id": "i", "type": "multi-repo-implement", "feature": "specs/001-demo"},
        make_context(step_module, central),
    )

    assert result.status == step_module.StepStatus.FAILED
    assert dispatch_calls == []
    assert "member path not found" in (result.error or "")


def test_validate_missing_and_dry_run_settings(step_module):
    errors = step_module.MultiRepoImplementStep().validate(
        {"id": "i", "type": "multi-repo-implement", "missing": "bogus", "dry_run": 3}
    )
    assert any("'missing' must be 'error' or 'skip'" in e for e in errors)
    assert any("'dry_run' must be" in e for e in errors)

    # Workflow-input expressions and boolean strings flow through.
    assert step_module.MultiRepoImplementStep().validate(
        {
            "id": "i",
            "type": "multi-repo-implement",
            "missing": "{{ inputs.missing }}",
            "dry_run": "{{ inputs.dry_run }}",
        }
    ) == []
    assert step_module.MultiRepoImplementStep().validate(
        {"id": "i", "type": "multi-repo-implement", "missing": "skip", "dry_run": True}
    ) == []


def test_validate_reports_config_errors(step_module):
    errors = step_module.MultiRepoImplementStep().validate(
        {
            "id": "i",
            "type": "multi-repo-implement",
            "bogus": True,
            "repos": "lambda",
            "max_concurrency": True,
            "dispatch_timeout": 0,
            "skip_completed": "yes",
        }
    )
    assert any("unknown fields" in e for e in errors)
    assert any("'repos' must be a list" in e for e in errors)
    assert any("'max_concurrency'" in e for e in errors)
    assert any("'dispatch_timeout'" in e for e in errors)
    assert any("'skip_completed'" in e for e in errors)


def test_validate_accepts_minimal_and_expression_configs(step_module):
    assert step_module.MultiRepoImplementStep().validate(
        {"id": "i", "type": "multi-repo-implement"}
    ) == []
    assert step_module.MultiRepoImplementStep().validate(
        {
            "id": "i",
            "type": "multi-repo-implement",
            "feature": "{{ inputs.feature }}",
            "max_concurrency": 3,
            "skip_completed": False,
        }
    ) == []


# -- objective verification, retry, watchdog, status, contract-check --------

ORCHESTRATION_PAGE = (
    "# Orchestration page\n\n"
    "| Member | Tasks file | Depends on | Status |\n"
    "|--------|------------|------------|--------|\n"
    "| s3 | tasks/s3.md | — | pending |\n"
    "| lambda | tasks/lambda.md | s3 | pending |\n"
)


def verified_tasks(*commands: str) -> str:
    lines = ["- [ ] T001 implement", "", "## Verification", ""]
    lines.extend(f"- {command}" for command in commands)
    return "\n".join(lines) + "\n"


class ScriptedIntegration(FakeIntegration):
    """FakeIntegration with a per-member side effect run on each dispatch."""

    def __init__(self, calls, effects):
        super().__init__(calls)
        self._effects = effects

    def dispatch_command(self, command_name, args="", *, project_root=None, **kwargs):
        result = super().dispatch_command(command_name, args, project_root=project_root, **kwargs)
        effect = self._effects.get(Path(str(project_root)).name)
        if effect is not None:
            return effect(Path(str(project_root)), args, kwargs) or result
        return result


def patch_scripted(monkeypatch, calls, effects):
    import specify_cli.integrations as integrations_module

    fake = ScriptedIntegration(calls, effects)
    monkeypatch.setattr(integrations_module, "get_integration", lambda key: fake)


def run_step(step_module, central, feature, **config):
    return step_module.MultiRepoImplementStep().execute(
        {"id": "implement", "type": "multi-repo-implement", "feature": str(feature), **config},
        make_context(step_module, central),
    )


def test_parse_verification_section(step_module):
    verification = step_module.verification
    text = (
        "- [ ] T001 task\n\n## Verification\n\n"
        "<!-- - ignored: `false` -->\n"
        "- lint: `npm run lint` → exit 0\n"
        "- `make test`\n"
        "- e2e: ``grep -q '`' f`` (expected exit code 1)\n"
        "```sh\n- not-a-check: `false`\n```\n"
        "## Notes\n- after: `false`\n"
    )
    commands = verification.parse_verification(text)
    assert [(c.name, c.command, c.expected_exit) for c in commands] == [
        ("lint", "npm run lint", 0),
        ("check-2", "make test", 0),
        ("e2e", "grep -q '`' f", 1),
    ]
    assert verification.parse_verification("- [ ] T001\n") is None
    assert verification.parse_verification("## Verification\n\nnone yet\n") == []


def test_verification_passing_records_verified_and_status(
    workspace_setup, step_module, monkeypatch, dispatch_calls
):
    central, members, feature = workspace_setup
    (feature / "tasks.md").write_text(ORCHESTRATION_PAGE, encoding="utf-8")
    (feature / "tasks" / "s3.md").write_text(
        verified_tasks(
            f'cwd: `test "$(pwd -P)" = "{members["s3"].resolve()}"`',
            "test: `true` → exit 0",
            "e2e: `exit 3` → exit 3",
        ),
        encoding="utf-8",
    )
    seen_status: list[str] = []

    def record_page(_root, _args, _kwargs):
        seen_status.append((feature / "tasks.md").read_text(encoding="utf-8"))

    patch_scripted(monkeypatch, dispatch_calls, {S3: record_page})

    result = run_step(step_module, central, feature)

    assert result.status == step_module.StepStatus.COMPLETED, result.error
    per_repo = {r["repo"]: r for r in result.output["results"]}
    s3 = per_repo["s3"]
    assert s3["status"] == "completed"
    assert s3["attempts"] == 1
    assert s3["verification"]["status"] == "verified"
    assert [c["status"] for c in s3["verification"]["commands"]] == ["passed"] * 3
    assert s3["verification"]["commands"][2]["exit_code"] == 3
    assert per_repo["lambda"]["verification"]["status"] == "skipped"
    assert result.output["verification"] == {"s3": "verified", "lambda": "skipped"}
    assert "verified: s3" in result.output["verification_summary"]
    log = feature / "verification" / "s3.log"
    assert log.is_file() and "=== test: true" in log.read_text(encoding="utf-8")
    report = (feature / "verification" / "report.md").read_text(encoding="utf-8")
    assert "| s3 | completed | verified | 1 | — |" in report
    assert result.output["verification_report"] == str(feature / "verification" / "report.md")

    # Status column: dispatched while the agent runs, then the outcome.
    assert "| s3 | tasks/s3.md | — | dispatched |" in seen_status[0]
    page = (feature / "tasks.md").read_text(encoding="utf-8")
    assert "| s3 | tasks/s3.md | — | verified |" in page
    assert "| lambda | tasks/lambda.md | s3 | completed |" in page
    assert page.startswith("# Orchestration page\n\n| Member | Tasks file |")


def test_verification_failure_retries_with_feedback(
    workspace_setup, step_module, monkeypatch, dispatch_calls
):
    central, members, feature = workspace_setup
    (feature / "tasks.md").write_text(ORCHESTRATION_PAGE, encoding="utf-8")
    (feature / "tasks" / "s3.md").write_text(
        verified_tasks("unit: `test -f fixed || { echo 'fixed missing'; exit 2; }`", "lint: `true`"),
        encoding="utf-8",
    )
    feedback_seen: list[str] = []

    def fix_on_feedback(root, args, _kwargs):
        parts = args.split(" ")
        if len(parts) == 3:  # tasks file, governance bundle, feedback file
            feedback_seen.append(Path(parts[2]).read_text(encoding="utf-8"))
            (root / "fixed").write_text("", encoding="utf-8")

    patch_scripted(monkeypatch, dispatch_calls, {S3: fix_on_feedback})

    result = run_step(step_module, central, feature, verify_retries=2)

    assert result.status == step_module.StepStatus.COMPLETED, result.error
    s3 = {r["repo"]: r for r in result.output["results"]}["s3"]
    assert s3["status"] == "completed"
    assert s3["attempts"] == 2
    assert s3["verification"]["status"] == "verified"
    assert s3["verification"]["runs"] == 2
    s3_calls = [c for c in dispatch_calls if c["member"] == S3]
    assert len(s3_calls) == 2
    assert len(s3_calls[0]["args"].split(" ")) == 2
    assert s3_calls[1]["args"].endswith("verification/s3.feedback.md")
    assert "## unit — exit 2, expected 0" in feedback_seen[0]
    assert "fixed missing" in feedback_seen[0]
    assert "Checks already passing: lint." in feedback_seen[0]
    assert "| s3 | tasks/s3.md | — | verified |" in (feature / "tasks.md").read_text(
        encoding="utf-8"
    )


def test_verification_failure_exhausts_retries(
    workspace_setup, step_module, monkeypatch, dispatch_calls
):
    central, members, feature = workspace_setup
    (feature / "tasks.md").write_text(ORCHESTRATION_PAGE, encoding="utf-8")
    (feature / "tasks" / "s3.md").write_text(verified_tasks("unit: `false`"), encoding="utf-8")
    patch_integration(monkeypatch, dispatch_calls)

    result = run_step(step_module, central, feature, verify_retries=1)

    assert result.status == step_module.StepStatus.FAILED
    assert "verification failed: unit" in result.error
    s3 = {r["repo"]: r for r in result.output["results"]}["s3"]
    assert s3["status"] == "failed"
    assert s3["attempts"] == 2
    assert s3["verification"]["status"] == "failed"
    assert "verification failed after 2 dispatch(es): unit" in s3["error"]
    assert [c["member"] for c in dispatch_calls] == [S3, S3]  # lambda never ran
    assert result.output["failed"] == ["s3"]
    assert result.output["verification"] == {"s3": "failed"}
    assert "| s3 | tasks/s3.md | — | failed |" in (feature / "tasks.md").read_text(
        encoding="utf-8"
    )


def test_default_verify_retries_is_zero(workspace_setup, step_module, monkeypatch, dispatch_calls):
    central, members, feature = workspace_setup
    (feature / "tasks" / "s3.md").write_text(verified_tasks("unit: `false`"), encoding="utf-8")
    patch_integration(monkeypatch, dispatch_calls)

    result = run_step(step_module, central, feature)

    assert result.status == step_module.StepStatus.FAILED
    assert len(dispatch_calls) == 1


def test_dispatch_failure_skips_verification(
    workspace_setup, step_module, monkeypatch, dispatch_calls
):
    central, members, feature = workspace_setup
    (feature / "tasks" / "s3.md").write_text(verified_tasks("unit: `true`"), encoding="utf-8")
    patch_integration(monkeypatch, dispatch_calls, fail_repos={S3: 4})

    result = run_step(step_module, central, feature, verify_retries=3)

    s3 = {r["repo"]: r for r in result.output["results"]}["s3"]
    assert s3["status"] == "failed"
    assert s3["exit_code"] == 4
    assert s3["verification"]["status"] == "skipped"
    assert len(dispatch_calls) == 1  # retries apply to verification only


def test_skip_completed_is_confirmed_by_verification(
    workspace_setup, step_module, monkeypatch, dispatch_calls
):
    central, members, feature = workspace_setup
    done = "- [x] T001 implement\n\n## Verification\n\n- unit: `test -f built`\n"
    (feature / "tasks" / "s3.md").write_text(done, encoding="utf-8")
    (feature / "tasks" / "lambda.md").write_text(
        done.replace("built", "never-there"), encoding="utf-8"
    )
    (members["s3"] / "built").write_text("", encoding="utf-8")
    patch_integration(monkeypatch, dispatch_calls)

    result = run_step(step_module, central, feature, skip_completed=True)

    per_repo = {r["repo"]: r for r in result.output["results"]}
    assert per_repo["s3"]["status"] == "skipped"
    assert per_repo["s3"]["verification"]["status"] == "verified"
    # Checked-off but failing checks: the agent is dispatched again with feedback.
    assert per_repo["lambda"]["status"] == "failed"
    assert [c["member"] for c in dispatch_calls] == [LAMBDA]
    assert dispatch_calls[0]["args"].endswith("verification/lambda.feedback.md")


def test_sequential_dispatch_timeout(workspace_setup, step_module, monkeypatch, dispatch_calls):
    import subprocess
    import time

    central, members, feature = workspace_setup

    def hang_like_a_streamed_agent(root, _args, kwargs):
        assert kwargs["stream"] is True
        # Streamed dispatches call subprocess.run without a timeout.
        completed = subprocess.run(["sh", "-c", "sleep 30"], cwd=root)
        return {"exit_code": completed.returncode, "stdout": "", "stderr": ""}

    patch_scripted(monkeypatch, dispatch_calls, {S3: hang_like_a_streamed_agent})

    started = time.monotonic()
    result = run_step(step_module, central, feature, dispatch_timeout=1)
    elapsed = time.monotonic() - started

    assert elapsed < 15
    assert result.status == step_module.StepStatus.FAILED
    assert "s3 (timed out)" in result.error
    s3 = {r["repo"]: r for r in result.output["results"]}["s3"]
    assert s3["status"] == "failed"
    assert s3["timed_out"] is True
    assert "dispatch_timeout" in s3["error"]
    # The watchdog is scoped to the dispatch.
    assert subprocess.run.__name__ == "run"


def test_verification_command_timeout(workspace_setup, step_module, monkeypatch, dispatch_calls):
    central, members, feature = workspace_setup
    (feature / "tasks" / "s3.md").write_text(verified_tasks("slow: `sleep 30`"), encoding="utf-8")
    patch_integration(monkeypatch, dispatch_calls)

    result = run_step(step_module, central, feature, dispatch_timeout=1)

    s3 = {r["repo"]: r for r in result.output["results"]}["s3"]
    check = s3["verification"]["commands"][0]
    assert check["timed_out"] is True
    assert check["status"] == "failed"
    assert s3["status"] == "failed"


def test_ready_false_holds_member_and_dependents(
    workspace_setup, step_module, monkeypatch, dispatch_calls
):
    central, members, feature = workspace_setup
    (feature / "tasks.md").write_text(ORCHESTRATION_PAGE, encoding="utf-8")
    (feature / "tasks" / "s3.md").write_text("---\nready: false\n---\n- [ ] T001\n", encoding="utf-8")
    patch_integration(monkeypatch, dispatch_calls)

    result = run_step(step_module, central, feature)

    assert result.status == step_module.StepStatus.COMPLETED, result.error
    assert dispatch_calls == []
    assert result.output["not_ready"] == ["lambda", "s3"]
    per_repo = {r["repo"]: r for r in result.output["results"]}
    assert per_repo["s3"]["status"] == "not-ready"
    assert "depends on not-ready member(s): s3" in per_repo["lambda"]["reason"]
    assert "pending" in (feature / "tasks.md").read_text(encoding="utf-8")


def test_ready_false_dispatches_the_rest(tmp_path, step_module, monkeypatch, dispatch_calls):
    central = write_workspace(
        tmp_path,
        members=[{"id": "s3", "path": "repo-s3"}, {"id": "glue", "path": "repo-glue"}],
    )
    make_member(tmp_path, "s3")
    make_member(tmp_path, "glue")
    feature = make_feature(
        central, task_files={"s3": "- [ ] T001\n", "glue": "---\nready: no\n---\n- [ ] T002\n"}
    )
    patch_integration(monkeypatch, dispatch_calls)

    result = run_step(step_module, central, feature)

    assert [c["member"] for c in dispatch_calls] == [S3]
    assert result.output["not_ready"] == ["glue"]
    assert result.output["waves"] == [["s3"]]


def test_contract_check_member_runs_after_producers(
    tmp_path, step_module, monkeypatch, dispatch_calls
):
    central = write_workspace(
        tmp_path,
        members=[
            {"id": "api", "path": "repo-api"},
            {"id": "web", "path": "repo-web", "depends_on": ["api"]},
            {
                "id": "contract-check",
                "path": "repo-contracts",
                "tasks_file": "tasks/contract-check.md",
                "depends_on": ["api"],
            },
        ],
    )
    make_member(tmp_path, "api")
    make_member(tmp_path, "web")
    contracts_repo = make_member(tmp_path, "contracts", with_specify=False)
    feature = make_feature(central, task_files={"api": "- [ ] T001 api\n", "web": "- [ ] T002 web\n"})
    (feature / "contracts").mkdir()
    (feature / "contracts" / "openapi.yaml").write_text("openapi: 3.1.0\n", encoding="utf-8")
    (feature / "tasks" / "contract-check.md").write_text(
        "# Contract check\n\n## Verification\n\n"
        '- schema-diff: `test -f "$SPECIFY_FEATURE_DIRECTORY/contracts/openapi.yaml"`\n'
        "- api-built: `test -f ../repo-api/built`\n",
        encoding="utf-8",
    )
    order: list[str] = []

    def build_api(root, _args, _kwargs):
        order.append("api")
        (root / "built").write_text("", encoding="utf-8")

    patch_scripted(monkeypatch, dispatch_calls, {"repo-api": build_api})

    result = run_step(step_module, central, feature)

    assert result.status == step_module.StepStatus.COMPLETED, result.error
    waves = result.output["waves"]
    assert waves[0] == ["api"]
    assert "contract-check" in waves[1]
    # Verification-only: no agent dispatch, only the contract commands.
    assert [c["member"] for c in dispatch_calls] == ["repo-api", "repo-web"]
    check = {r["repo"]: r for r in result.output["results"]}["contract-check"]
    assert check["mode"] == "verify-only"
    assert check["status"] == "completed"
    assert check["verification"]["status"] == "verified"
    members = {m["repo"]: m for m in result.output["members"]}
    assert members["contract-check"]["status"] == "ready"  # plain repo is fine
    assert members["contract-check"]["verify_only"] is True
    assert contracts_repo.is_dir()


def test_contract_check_failure_fails_the_run(tmp_path, step_module, monkeypatch, dispatch_calls):
    central = write_workspace(
        tmp_path,
        members=[
            {"id": "api", "path": "repo-api"},
            {
                "id": "contract-check",
                "path": "repo-api",
                "tasks_file": "tasks/contract-check.md",
                "depends_on": ["api"],
            },
        ],
    )
    make_member(tmp_path, "api")
    feature = make_feature(central, task_files={"api": "- [ ] T001 api\n"})
    (feature / "tasks" / "contract-check.md").write_text(
        "## Verification\n\n- consumer-contract: `exit 1`\n", encoding="utf-8"
    )
    patch_integration(monkeypatch, dispatch_calls)

    result = run_step(step_module, central, feature, verify_retries=2)

    assert result.status == step_module.StepStatus.FAILED
    assert result.output["waves"] == [["api"], ["contract-check"]]
    check = {r["repo"]: r for r in result.output["results"]}["contract-check"]
    assert check["verification"]["status"] == "failed"
    assert check["verification"]["runs"] == 1  # no agent to retry
    assert len(dispatch_calls) == 1


def test_update_orchestration_status_preserves_page(step_module, tmp_path):
    verification = step_module.verification
    page = tmp_path / "tasks.md"
    page.write_text(
        "# Page\n\n| Status | Member |\n| --- | --- |\n| pending | `s3` |\n\ntext | s3 | x\n",
        encoding="utf-8",
    )
    assert verification.update_orchestration_status(page, "s3", "dispatched") is True
    assert page.read_text(encoding="utf-8") == (
        "# Page\n\n| Status | Member |\n| --- | --- |\n| dispatched | `s3` |\n\ntext | s3 | x\n"
    )
    assert verification.update_orchestration_status(page, "nope", "failed") is False
    assert verification.update_orchestration_status(tmp_path / "absent.md", "s3", "x") is False


def test_validate_rejects_bad_verify_retries(step_module):
    step = step_module.MultiRepoImplementStep()
    for bad in (-1, "two", True, 1.5):
        errors = step.validate({"id": "implement", "type": "multi-repo-implement", "verify_retries": bad})
        assert any("verify_retries" in e for e in errors), bad
    for good in (0, 3, "{{ inputs.verify_retries }}"):
        errors = step.validate({"id": "implement", "type": "multi-repo-implement", "verify_retries": good})
        assert not any("verify_retries" in e for e in errors), good


def test_empty_verification_only_member_is_skipped_not_verified(
    tmp_path, step_module, monkeypatch, dispatch_calls
):
    central = write_workspace(
        tmp_path,
        members=[
            {"id": "api", "path": "repo-api"},
            {
                "id": "contract-check",
                "path": "repo-api",
                "tasks_file": "tasks/contract-check.md",
                "depends_on": ["api"],
            },
        ],
    )
    make_member(tmp_path, "api")
    feature = make_feature(central, task_files={"api": "- [ ] T001 api\n"})
    (feature / "tasks" / "contract-check.md").write_text(
        "## Verification\n\n<!-- no automated checks: contract not finalized -->\n",
        encoding="utf-8",
    )
    patch_integration(monkeypatch, dispatch_calls)

    result = run_step(step_module, central, feature)

    assert result.status == step_module.StepStatus.COMPLETED, result.error
    assert [c["member"] for c in dispatch_calls] == ["repo-api"]  # no agent for the check
    check = {r["repo"]: r for r in result.output["results"]}["contract-check"]
    assert check["mode"] == "verify-only"
    assert check["status"] == "skipped"
    assert check["verification"]["status"] == "skipped"
    assert result.output["verification"]["contract-check"] == "skipped"


def test_sequential_timeout_kills_agent_descendants(
    workspace_setup, step_module, monkeypatch, dispatch_calls
):
    import subprocess
    import time

    central, members, feature = workspace_setup
    late = members["s3"] / "late-write"

    def agent_with_background_child(root, _args, _kwargs):
        # The agent spawns a child that writes after the deadline, then hangs.
        completed = subprocess.run(
            ["sh", "-c", "(sleep 2; : > late-write) & sleep 30"], cwd=root
        )
        return {"exit_code": completed.returncode, "stdout": "", "stderr": ""}

    patch_scripted(monkeypatch, dispatch_calls, {S3: agent_with_background_child})

    result = run_step(step_module, central, feature, dispatch_timeout=1)

    s3 = {r["repo"]: r for r in result.output["results"]}["s3"]
    assert s3["timed_out"] is True
    time.sleep(2.5)
    assert not late.exists()


def test_run_with_watchdog_matches_subprocess_run(step_module):
    import subprocess

    completed = step_module.run_with_watchdog(
        ["sh", "-c", "cat; echo err >&2; exit 3"],
        timeout=10,
        input="hi",
        capture_output=True,
        text=True,
    )
    assert (completed.returncode, completed.stdout, completed.stderr) == (3, "hi", "err\n")
    with pytest.raises(subprocess.CalledProcessError):
        step_module.run_with_watchdog(["sh", "-c", "exit 1"], timeout=10, check=True)
    with pytest.raises(subprocess.TimeoutExpired):
        step_module.run_with_watchdog(["sh", "-c", "sleep 30"], timeout=0.5)


def test_watchdog_keeps_agent_in_orchestrator_process_group(step_module, tmp_path):
    import os

    with step_module.streaming_watchdog(10):
        step_module.subprocess.run(
            ["sh", "-c", "ps -o pgid= -p $$ > pgid"], cwd=tmp_path
        )
    assert int((tmp_path / "pgid").read_text()) == os.getpgrp()


def test_watchdog_kills_descendants_in_another_session(step_module, tmp_path):
    import subprocess
    import sys
    import time

    # The agent's child moves to its own session (out of the agent's process
    # group) and writes after the deadline; the tree walk still reaches it.
    spawn_detached = (
        "import subprocess; subprocess.Popen("
        "['sh', '-c', 'sleep 2; : > late-write'], start_new_session=True)"
        ".wait()"
    )
    with pytest.raises(subprocess.TimeoutExpired):
        with step_module.streaming_watchdog(1):
            step_module.subprocess.run(
                ["sh", "-c", f'"{sys.executable}" -c "{spawn_detached}"; sleep 30'],
                cwd=tmp_path,
            )
    time.sleep(2.5)
    assert not (tmp_path / "late-write").exists()
