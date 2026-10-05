"""Shared fixtures for the multi-repo extension test suite.

Run with the spec-kit checkout's virtualenv::

    uv sync --extra test
    .venv/bin/python -m pytest multi-repo/tests
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
STEP_DIR = PACKAGE_ROOT / "step" / "multi-repo-implement"


def load_step_package():
    """Import the step package by path (its directory name has a hyphen).

    Mirrors how the workflow engine loads installed custom steps: a synthetic
    module with submodule search locations, so the relative ``.workspace``
    import resolves.
    """
    module_name = "multi_repo_implement_step_under_test"
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(
        module_name,
        STEP_DIR / "__init__.py",
        submodule_search_locations=[str(STEP_DIR)],
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def load_workspace_module():
    return load_step_package().workspace


class FakeIntegration:
    """Stands in for a real integration in dispatch tests.

    Records every dispatch_command call for assertions; ``fail_repos`` maps
    member id -> exit code so tests can simulate failures.
    """

    key = "fake"

    def __init__(self, calls: list[dict[str, Any]], fail_repos: dict[str, int] | None = None):
        self._calls = calls
        self._fail_repos = fail_repos or {}

    def build_exec_args(self, prompt, **_kwargs):
        return ["fake-agent", "-p", prompt]

    def dispatch_command(self, command_name, args="", *, project_root=None, **kwargs):
        member_id = Path(str(project_root)).name
        self._calls.append(
            {
                "command": command_name,
                "args": args,
                "project_root": str(project_root),
                "stream": kwargs.get("stream"),
                "model": kwargs.get("model"),
                "member": member_id,
            }
        )
        exit_code = self._fail_repos.get(member_id, 0)
        return {"exit_code": exit_code, "stdout": "", "stderr": "boom" if exit_code else ""}


def write_workspace(
    root: Path,
    members: list[dict[str, Any]],
    workspace_id: str = "platform",
) -> Path:
    """Write a workspace manifest + minimal central project scaffolding."""
    specify = root / ".specify"
    specify.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": "1.0",
        "workspace": {"id": workspace_id, "name": "Platform"},
        "members": members,
    }
    (specify / "workspace.yml").write_text(json.dumps(manifest), encoding="utf-8")
    return root


def make_member(root: Path, member_id: str, *, with_specify: bool = True) -> Path:
    """Create a member repository directory with an integration.json."""
    member = root / f"repo-{member_id}"
    if with_specify:
        specify = member / ".specify"
        specify.mkdir(parents=True, exist_ok=True)
        (specify / "integration.json").write_text(
            json.dumps({"integration": "fake"}), encoding="utf-8"
        )
    else:
        member.mkdir(parents=True, exist_ok=True)
    return member


def make_feature(
    central: Path,
    slug: str = "001-demo",
    task_files: dict[str, str] | None = None,
) -> Path:
    """Create a central feature directory with spec/tasks and member slices."""
    (central / ".specify").mkdir(parents=True, exist_ok=True)
    feature = central / "specs" / slug
    feature.mkdir(parents=True, exist_ok=True)
    (feature / "spec.md").write_text("# Spec\n", encoding="utf-8")
    (feature / "tasks.md").write_text("# Orchestration page\n", encoding="utf-8")
    tasks_dir = feature / "tasks"
    tasks_dir.mkdir(exist_ok=True)
    if task_files is None:
        task_files = {"s3": "", "lambda": ""}
    for name, body in task_files.items():
        (tasks_dir / f"{name}.md").write_text(body, encoding="utf-8")
    return feature


@pytest.fixture()
def step_module():
    return load_step_package()


@pytest.fixture()
def workspace_module():
    return load_workspace_module()


@pytest.fixture()
def dispatch_calls():
    return []
