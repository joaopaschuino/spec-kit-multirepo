"""Tests for the workspace manifest parser, validator, and wave builder."""

from __future__ import annotations

import json

import pytest

from conftest import make_feature, make_member, write_workspace


def test_load_workspace_parses_defaults(tmp_path, workspace_module):
    central = write_workspace(
        tmp_path / "central",
        members=[
            {"id": "lambda", "path": "../repo-lambda"},
            {"id": "s3", "path": "repo-s3", "integration": "claude"},
        ],
    )
    make_member(tmp_path, "lambda")
    make_member(tmp_path, "s3")

    ws = workspace_module.load_workspace(central)

    assert ws.id == "platform"
    assert ws.name == "Platform"
    assert ws.root == central.resolve()
    assert [m.id for m in ws.members] == ["lambda", "s3"]

    lam = ws.member("lambda")
    assert lam is not None
    assert lam.integration is None  # falls back to member's integration.json
    assert lam.tasks_file == "tasks/lambda.md"  # default derived from id
    assert lam.depends_on == ()
    assert lam.path == (tmp_path / "repo-lambda").resolve()

    s3 = ws.member("s3")
    assert s3 is not None
    assert s3.integration == "claude"
    assert s3.tasks_file == "tasks/s3.md"


def test_load_workspace_missing_manifest(tmp_path, workspace_module):
    with pytest.raises(workspace_module.WorkspaceError, match="workspace manifest"):
        workspace_module.load_workspace(tmp_path)


@pytest.mark.parametrize(
    "payload,match",
    [
        ({"schema_version": "2.0"}, "schema_version"),
        ({"schema_version": "1.0"}, "'workspace' must be a mapping"),
        ({"schema_version": "1.0", "workspace": {"id": "x"}, "members": []}, "non-empty list"),
        (
            {"schema_version": "1.0", "workspace": {"id": "x", "bogus": 1}, "members": [{"id": "a", "path": "a"}]},
            "unknown 'workspace' keys",
        ),
        (
            {"schema_version": "1.0", "workspace": {"id": "x"}, "members": [{"id": "a"}]},
            "requires a non-empty 'path'",
        ),
        (
            {"schema_version": "1.0", "workspace": {"id": "x"}, "members": [{"path": "a", "id": "A"}]},
            "must match",
        ),
        (
            {
                "schema_version": "1.0",
                "workspace": {"id": "x"},
                "members": [
                    {"id": "a", "path": "a"},
                    {"id": "a", "path": "b"},
                ],
            },
            "duplicate member id",
        ),
        (
            {
                "schema_version": "1.0",
                "workspace": {"id": "x"},
                "members": [{"id": "a", "path": "a", "depends_on": ["ghost"]}],
            },
            "unknown member 'ghost'",
        ),
        (
            {
                "schema_version": "1.0",
                "workspace": {"id": "x"},
                "members": [{"id": "a", "path": "a", "depends_on": ["a"]}],
            },
            "cannot depend on itself",
        ),
        (
            {
                "schema_version": "1.0",
                "workspace": {"id": "x"},
                "members": [
                    {"id": "a", "path": "a", "depends_on": ["b"]},
                    {"id": "b", "path": "b", "depends_on": ["a"]},
                ],
            },
            "cycle detected: a -> b -> a",
        ),
        (
            {
                "schema_version": "1.0",
                "workspace": {"id": "x"},
                "members": [{"id": "a", "path": "a", "constitution": 42}],
            },
            "'constitution' must be",
        ),
        (
            {
                "schema_version": "1.0",
                "workspace": {"id": "x"},
                "members": [{"id": "a", "path": "a", "tasks_file": 42}],
            },
            "'tasks_file' must be a non-empty string",
        ),
        (
            {
                "schema_version": "1.0",
                "workspace": {"id": "x"},
                "members": [{"id": "a", "path": "a", "surprise": True}],
            },
            "unknown keys",
        ),
    ],
)
def test_load_workspace_rejects_invalid_manifests(tmp_path, workspace_module, payload, match):
    central = tmp_path / "central"
    specify = central / ".specify"
    specify.mkdir(parents=True)
    (specify / "workspace.yml").write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(workspace_module.WorkspaceError, match=match):
        workspace_module.load_workspace(central)


def test_topological_waves_diamond(workspace_module):
    graph = {
        "a": (),
        "b": ("a",),
        "c": ("a",),
        "d": ("b", "c"),
    }
    assert workspace_module.topological_waves(["a", "b", "c", "d"], graph) == [
        ["a"],
        ["b", "c"],
        ["d"],
    ]


def test_topological_waves_ignores_non_participant_dependencies(workspace_module):
    # Feature touches only c and d. d depends on b, which is not in this
    # feature, so the edge is dropped and d joins wave 0 with c.
    graph = {"a": (), "b": ("a",), "c": ("a",), "d": ("b",)}
    assert workspace_module.topological_waves(["c", "d"], graph) == [["c", "d"]]


def test_topological_waves_sorted_and_deterministic(workspace_module):
    graph = {"z": (), "a": (), "m": ()}
    waves = workspace_module.topological_waves(["m", "z", "a"], graph)
    assert waves == [["a", "m", "z"]]


def test_workspace_waves_end_to_end(tmp_path, workspace_module):
    central = write_workspace(
        tmp_path,
        members=[
            {"id": "s3", "path": "repo-s3"},
            {"id": "glue", "path": "repo-glue", "depends_on": ["s3"]},
            {"id": "lambda", "path": "repo-lambda", "depends_on": ["s3"]},
            {"id": "step-functions", "path": "repo-step-functions", "depends_on": ["lambda", "glue"]},
        ],
    )
    ws = workspace_module.load_workspace(central)
    assert workspace_module.workspace_waves(ws, ["s3", "glue", "lambda", "step-functions"]) == [
        ["s3"],
        ["glue", "lambda"],
        ["step-functions"],
    ]


def test_load_workspace_constitution_resolution(tmp_path, workspace_module):
    central = write_workspace(
        tmp_path / "central",
        members=[
            {"id": "lambda", "path": "../repo-lambda", "constitution": "constitutions/lambda.md"},
            {"id": "s3", "path": "../repo-s3", "constitution": str(tmp_path / "abs-type.md")},
            {"id": "glue", "path": "../repo-glue"},
        ],
    )
    make_member(tmp_path, "lambda")
    make_member(tmp_path, "s3")
    make_member(tmp_path, "glue")

    ws = workspace_module.load_workspace(central)

    lam = ws.member("lambda")
    assert lam.constitution == (tmp_path / "central" / "constitutions" / "lambda.md").resolve()
    s3 = ws.member("s3")
    assert s3.constitution == (tmp_path / "abs-type.md").resolve()  # absolute kept
    glue = ws.member("glue")
    assert glue.constitution is None  # optional

    # Two members of the same type share one file by pointing at it.
    shared = write_workspace(
        tmp_path / "central2",
        members=[
            {"id": "lambda-a", "path": "../repo-lambda-a", "constitution": "constitutions/lambda.md"},
            {"id": "lambda-b", "path": "../repo-lambda-b", "constitution": "constitutions/lambda.md"},
        ],
    )
    ws2 = workspace_module.load_workspace(shared)
    assert ws2.member("lambda-a").constitution == ws2.member("lambda-b").constitution


def test_read_feature_directory_priority(tmp_path, workspace_module, monkeypatch):
    central = tmp_path / "central"
    feature = make_feature(central)
    monkeypatch.delenv("SPECIFY_FEATURE_DIRECTORY", raising=False)

    # feature.json fallback, relative path resolved against the central root.
    (central / ".specify" / "feature.json").write_text(
        json.dumps({"feature_directory": "specs/001-demo"}), encoding="utf-8"
    )
    assert workspace_module.read_feature_directory(central) == "specs/001-demo"

    # Environment wins over feature.json, and may be absolute.
    monkeypatch.setenv("SPECIFY_FEATURE_DIRECTORY", str(feature))
    assert workspace_module.read_feature_directory(central) == str(feature)


def test_read_feature_directory_missing(tmp_path, workspace_module, monkeypatch):
    monkeypatch.delenv("SPECIFY_FEATURE_DIRECTORY", raising=False)
    with pytest.raises(workspace_module.WorkspaceError, match="No active feature"):
        workspace_module.read_feature_directory(tmp_path)
