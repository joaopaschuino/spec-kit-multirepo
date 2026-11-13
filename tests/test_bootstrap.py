"""Tests for the workspace bootstrap (clone absent members from their url)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from conftest import load_step_package, write_workspace


def git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout


def make_remote(root: Path, name: str, *, spec_kit: bool = True) -> Path:
    """A local git repository with one commit, usable as a clone source."""
    repo = root / "remotes" / name
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text(f"# {name}\n", encoding="utf-8")
    if spec_kit:
        (repo / ".specify").mkdir()
        (repo / ".specify" / "integration.json").write_text('{"integration": "fake"}', encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "init")
    return repo


@pytest.fixture()
def bootstrap_module():
    return load_step_package().bootstrap


def test_bootstrap_clones_skips_and_reports(tmp_path, workspace_module, bootstrap_module):
    """Clone from a path and a file:// url, skip present members, report url-less ones."""
    s3_remote = make_remote(tmp_path, "s3")
    web_remote = make_remote(tmp_path, "web", spec_kit=False)
    central = tmp_path / "central"
    central.mkdir()
    git(central, "init", "-q", "-b", "main")
    write_workspace(
        central,
        members=[
            {"id": "s3", "path": "../repo-s3", "url": str(s3_remote)},
            {"id": "web", "path": "apps/web", "url": web_remote.as_uri()},
            {"id": "glue", "path": "../repo-glue"},  # absent, no url
            {"id": "lambda", "path": "../repo-lambda", "url": "file:///nowhere"},  # present
        ],
    )
    (tmp_path / "repo-lambda").mkdir()

    result = bootstrap_module.bootstrap_workspace(workspace_module.load_workspace(central))

    assert result["cloned"] == ["s3", "web"]
    assert result["present"] == ["lambda"]
    assert result["missing"] == ["glue"]
    assert (tmp_path / "repo-s3" / "README.md").read_text(encoding="utf-8") == "# s3\n"
    assert (central / "apps" / "web" / "README.md").is_file()
    assert not any((tmp_path / "repo-lambda").iterdir())  # present member untouched

    members = {m["repo"]: m for m in result["members"]}
    assert members["s3"]["status"] == "cloned"
    assert members["s3"]["initialized"] is True
    assert members["web"]["initialized"] is False
    assert members["glue"]["status"] == "missing-url"
    assert "declares no 'url'" in members["glue"]["reason"]
    assert members["lambda"]["status"] == "present"

    # Only the clone inside the central repo is git-ignored; siblings are not.
    assert result["gitignore"] == ["/apps/web/"]
    gitignore = (central / ".gitignore").read_text(encoding="utf-8")
    assert "/apps/web/\n" in gitignore
    assert "repo-s3" not in gitignore
    status = git(central, "status", "--porcelain", "--untracked-files=all")
    assert "apps/" not in status  # no embedded repository to record

    # Re-running is idempotent: everything present, nothing new ignored.
    again = bootstrap_module.bootstrap_workspace(workspace_module.load_workspace(central))
    assert again["cloned"] == []
    assert again["present"] == ["s3", "web", "lambda"]
    assert again["gitignore"] == []
    assert (central / ".gitignore").read_text(encoding="utf-8") == gitignore


def test_bootstrap_respects_existing_ignores_and_monorepo_members(
    tmp_path, workspace_module, bootstrap_module
):
    api_remote = make_remote(tmp_path, "api")
    web_remote = make_remote(tmp_path, "web")
    central = tmp_path / "central"
    central.mkdir()
    git(central, "init", "-q", "-b", "main")
    (central / ".gitignore").write_text("node_modules/\napps/", encoding="utf-8")
    (central / "services" / "worker").mkdir(parents=True)  # mono-repo stack
    write_workspace(
        central,
        members=[
            {"id": "web", "path": "apps/web", "url": str(web_remote)},
            {"id": "api", "path": "services/api", "url": str(api_remote)},
            {"id": "worker", "path": "services/worker"},
        ],
    )

    result = bootstrap_module.bootstrap_workspace(workspace_module.load_workspace(central))

    assert result["cloned"] == ["web", "api"]
    assert result["gitignore"] == ["/services/api/"]  # apps/ already covers web
    assert (central / ".gitignore").read_text(encoding="utf-8") == (
        "node_modules/\napps/\n"
        f"\n{bootstrap_module.GITIGNORE_HEADER}\n/services/api/\n"
    )


def test_bootstrap_outside_git_work_tree_matches_gitignore_lines(
    tmp_path, workspace_module, bootstrap_module
):
    web_remote = make_remote(tmp_path, "web")
    api_remote = make_remote(tmp_path, "api")
    central = write_workspace(
        tmp_path / "central",
        members=[
            {"id": "web", "path": "apps/web", "url": str(web_remote)},
            {"id": "api", "path": "services/api", "url": str(api_remote)},
        ],
    )
    (central / ".gitignore").write_text("/apps/web/\n", encoding="utf-8")

    result = bootstrap_module.bootstrap_workspace(workspace_module.load_workspace(central))

    assert result["cloned"] == ["web", "api"]
    assert result["gitignore"] == ["/services/api/"]


def test_bootstrap_clone_failure_aborts(tmp_path, workspace_module, bootstrap_module):
    s3_remote = make_remote(tmp_path, "s3")
    central = write_workspace(
        tmp_path / "central",
        members=[
            {"id": "s3", "path": "s3", "url": str(s3_remote)},
            {"id": "lambda", "path": "lambda/repo", "url": str(tmp_path / "no-such-remote")},
            {"id": "glue", "path": "glue/deep/repo", "url": str(s3_remote)},
        ],
    )

    with pytest.raises(bootstrap_module.BootstrapError) as excinfo:
        bootstrap_module.bootstrap_workspace(workspace_module.load_workspace(central))

    message = str(excinfo.value)
    assert "member 'lambda' failed" in message
    assert "no-such-remote" in message
    result = excinfo.value.result
    assert result["cloned"] == ["s3"]  # cloned before the failure: kept
    assert result["failed"] == ["lambda"]
    members = {m["repo"]: m for m in result["members"]}
    assert members["glue"]["status"] == "not-attempted"
    assert not (central / "glue" / "deep" / "repo").exists()
    # The completed clone is still git-ignored.
    assert result["gitignore"] == ["/s3/"]
    assert "/s3/" in (central / ".gitignore").read_text(encoding="utf-8")


def test_bootstrap_dry_run_changes_nothing(tmp_path, workspace_module, bootstrap_module):
    web_remote = make_remote(tmp_path, "web")
    central = write_workspace(
        tmp_path / "central",
        members=[
            {"id": "web", "path": "apps/web", "url": str(web_remote)},
            {"id": "glue", "path": "../repo-glue"},
        ],
    )

    result = bootstrap_module.bootstrap_workspace(
        workspace_module.load_workspace(central), dry_run=True
    )

    members = {m["repo"]: m for m in result["members"]}
    assert members["web"]["status"] == "would-clone"
    assert result["cloned"] == []
    assert result["missing"] == ["glue"]
    assert result["gitignore"] == ["/apps/web/"]  # would be added
    assert not (central / "apps").exists()
    assert not (central / ".gitignore").exists()


def test_origin_url(tmp_path, bootstrap_module):
    remote = make_remote(tmp_path, "s3")
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", str(remote), str(clone))
    (clone / "sub").mkdir()
    no_origin = make_remote(tmp_path, "local")

    assert bootstrap_module.origin_url(clone) == str(remote)
    # A subdirectory (mono-repo member) is not its own repository.
    assert bootstrap_module.origin_url(clone / "sub") is None
    assert bootstrap_module.origin_url(no_origin) is None
    assert bootstrap_module.origin_url(tmp_path / "absent") is None


def test_step_bootstrap_mode(tmp_path, step_module):
    s3_remote = make_remote(tmp_path, "s3")
    central = write_workspace(
        tmp_path / "central",
        members=[
            {"id": "s3", "path": "../repo-s3", "url": str(s3_remote)},
            {"id": "glue", "path": "../repo-glue"},
        ],
    )
    context = step_module.StepContext(project_root=str(central), inputs={})
    step = step_module.MultiRepoImplementStep()

    preview = step.execute({"id": "b", "type": "multi-repo-implement", "bootstrap": True, "dry_run": True}, context)
    assert preview.status == step_module.StepStatus.COMPLETED
    assert preview.output["dry_run"] is True
    assert not (tmp_path / "repo-s3").exists()

    result = step.execute({"id": "b", "type": "multi-repo-implement", "bootstrap": True}, context)
    assert result.status == step_module.StepStatus.COMPLETED
    assert result.output["bootstrap"] is True
    assert result.output["cloned"] == ["s3"]
    assert result.output["missing"] == ["glue"]
    assert (tmp_path / "repo-s3" / ".specify" / "integration.json").is_file()


def test_step_bootstrap_failure(tmp_path, step_module):
    central = write_workspace(
        tmp_path / "central",
        members=[{"id": "s3", "path": "../repo-s3", "url": str(tmp_path / "missing-remote")}],
    )
    result = step_module.MultiRepoImplementStep().execute(
        {"id": "b", "type": "multi-repo-implement", "bootstrap": "{{ inputs.bootstrap }}"},
        step_module.StepContext(project_root=str(central), inputs={"bootstrap": "true"}),
    )
    assert result.status == step_module.StepStatus.FAILED
    assert "Bootstrap of member 's3' failed" in result.error
    assert result.output["failed"] == ["s3"]


def test_step_validate_bootstrap(step_module):
    errors = step_module.MultiRepoImplementStep().validate(
        {"id": "b", "type": "multi-repo-implement", "bootstrap": 3}
    )
    assert any("'bootstrap' must be" in e for e in errors)
    assert not step_module.MultiRepoImplementStep().validate(
        {"id": "b", "type": "multi-repo-implement", "bootstrap": True}
    )


def test_dry_run_reports_url_and_suggests_origin(tmp_path, step_module):
    remote = make_remote(tmp_path, "s3")
    git(tmp_path, "clone", "-q", str(remote), str(tmp_path / "repo-s3"))
    git(tmp_path, "clone", "-q", str(remote), str(tmp_path / "repo-lambda"))
    central = write_workspace(
        tmp_path / "central",
        members=[
            {"id": "s3", "path": "../repo-s3"},
            {"id": "lambda", "path": "../repo-lambda", "url": "git@example.com:acme/lambda.git"},
            {"id": "glue", "path": "../repo-glue", "url": "git@example.com:acme/glue.git"},
        ],
    )

    result = step_module.MultiRepoImplementStep().execute(
        {"id": "c", "type": "multi-repo-implement", "dry_run": True, "repos": ["s3", "lambda", "glue"]},
        step_module.StepContext(project_root=str(central), inputs={}),
    )

    members = {m["repo"]: m for m in result.output["members"]}
    assert members["s3"]["suggested_url"] == str(remote)
    assert "url" not in members["s3"]
    assert members["lambda"]["url"] == "git@example.com:acme/lambda.git"
    assert "suggested_url" not in members["lambda"]
    assert members["glue"]["status"] == "not-cloned"
    assert "multi-repo-bootstrap" in members["glue"]["reason"]


def test_clone_runs_without_a_terminal(tmp_path, workspace_module, bootstrap_module, monkeypatch):
    """SSH prompts on the controlling tty regardless of GIT_TERMINAL_PROMPT."""
    calls = []
    real_run = subprocess.run

    def recording_run(args, **kwargs):
        if args[:2] == ["git", "clone"]:
            calls.append(kwargs)
        return real_run(args, **kwargs)

    monkeypatch.setattr(bootstrap_module.subprocess, "run", recording_run)
    remote = make_remote(tmp_path, "s3")
    central = write_workspace(
        tmp_path / "central", members=[{"id": "s3", "path": "../repo-s3", "url": str(remote)}]
    )

    bootstrap_module.bootstrap_workspace(workspace_module.load_workspace(central))

    (kwargs,) = calls
    assert kwargs["start_new_session"] is True
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert kwargs["env"]["SSH_ASKPASS_REQUIRE"] == "never"


def test_origin_url_resolves_relative_local_remote(tmp_path, bootstrap_module):
    remote = make_remote(tmp_path, "s3")
    member = make_remote(tmp_path, "member")
    git(member, "remote", "add", "origin", "../s3")
    scp = make_remote(tmp_path, "scp")
    git(scp, "remote", "add", "origin", "git@github.com:acme/scp.git")

    assert bootstrap_module.origin_url(member) == str(remote.resolve())
    assert bootstrap_module.origin_url(scp) == "git@github.com:acme/scp.git"


def test_gitignore_entries_escape_glob_characters(tmp_path, workspace_module, bootstrap_module):
    remote = make_remote(tmp_path, "web")
    central = tmp_path / "central"
    central.mkdir()
    git(central, "init", "-q", "-b", "main")
    write_workspace(
        central, members=[{"id": "web", "path": "apps/web[old]", "url": str(remote)}]
    )

    result = bootstrap_module.bootstrap_workspace(workspace_module.load_workspace(central))

    assert result["gitignore"] == ["/apps/web\\[old]/"]
    assert "apps/" not in git(central, "status", "--porcelain", "--untracked-files=all")
    (central / "apps" / "webo").mkdir()
    (central / "apps" / "webo" / "f").write_text("x", encoding="utf-8")
    assert "apps/webo/f" in git(central, "status", "--porcelain", "--untracked-files=all")

    # Outside a git work tree the escaped line is still recognised.
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / ".gitignore").write_text("/apps/web\\[old]/\n", encoding="utf-8")
    member = workspace_module.Member(id="web", path=plain / "apps" / "web[old]", url="x")
    assert bootstrap_module.ensure_gitignored(plain, [member], write=False) == []
