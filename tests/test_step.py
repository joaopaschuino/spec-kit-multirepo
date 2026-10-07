"""Tests for the multi-repo-implement workflow step."""

from __future__ import annotations

import json
import os

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
