"""Workspace manifest parsing for the multi-repo extension.

A *workspace* is a central specs repository plus a set of member
repositories, declared in ``.specify/workspace.yml`` at the central
project root::

    schema_version: "1.0"
    workspace:
      id: my-platform
      name: "My Platform"
    members:
      - id: lambda
        path: ../repo-lambda
        integration: claude        # optional; default: member's integration.json
        tasks_file: tasks/lambda.md  # optional; default: tasks/<id>.md
        depends_on: []               # member ids that must complete first
        domain: billing              # optional; business domain of the member
        communicates_with: [s3]      # optional; members it talks to at runtime

``depends_on`` is the execution graph (dispatch order, must be acyclic);
``communicates_with`` is the domain's communication topology (informational,
may contain cycles) and never affects dispatch.

The manifest is deliberately shaped as a candidate native contract: the
``path``/``depends_on``/``tasks_file`` model maps one-to-one onto a future
core ``project:``/``depends_on`` step field, so the extension can migrate to
core without a format break.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

#: Where the manifest lives, relative to the central project root.
WORKSPACE_FILE = ".specify/workspace.yml"

#: Supported manifest schema version (compare as string; YAML unquoted 1.0
#: parses as float, so normalize through str() to accept both spellings).
WORKSPACE_SCHEMA_VERSION = "1.0"

_MEMBER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

_ALLOWED_WORKSPACE_KEYS = {"id", "name"}
_ALLOWED_MEMBER_KEYS = {
    "id",
    "path",
    "integration",
    "tasks_file",
    "depends_on",
    "constitution",
    "domain",
    "communicates_with",
}


class WorkspaceError(ValueError):
    """The workspace manifest is missing, malformed, or semantically invalid."""


@dataclass(frozen=True)
class Member:
    """One member repository of the workspace."""

    id: str
    #: Absolute, resolved member project root.
    path: Path
    #: Integration key override; ``None`` means "read the member's own
    #: ``.specify/integration.json``".
    integration: str | None = None
    #: Member tasks file, relative to a feature directory.
    tasks_file: str = ""
    #: Optional member (or repo-type) constitution file, resolved against the
    #: central project root. Shared by every member pointing at the same
    #: file, so repositories of the same type (lambda, glue, ...) read one
    #: shared governance document instead of copies.
    constitution: Path | None = None
    #: Member ids whose dispatch must complete before this member's.
    depends_on: tuple[str, ...] = ()
    #: Optional business domain the member belongs to (e.g. ``billing``).
    domain: str | None = None
    #: Member ids this member communicates with at runtime. Describes the
    #: domain topology only; it may contain cycles and never orders dispatch.
    communicates_with: tuple[str, ...] = ()


@dataclass(frozen=True)
class Workspace:
    """A parsed workspace manifest."""

    id: str
    name: str
    #: Central project root the manifest was loaded from.
    root: Path
    members: tuple[Member, ...]

    def member(self, member_id: str) -> Member | None:
        """Return the member with the given id, or ``None``."""
        for member in self.members:
            if member.id == member_id:
                return member
        return None


def load_workspace(project_root: Path) -> Workspace:
    """Load and structurally validate the workspace manifest.

    Structural validation (unique ids, known dependencies, acyclic graph)
    happens here; filesystem checks (does the path exist, is it a Spec Kit
    project) are the caller's job so that unit tests can build workspaces
    purely from data.

    Raises :class:`WorkspaceError` with an actionable message on any problem.
    """
    root = Path(project_root)
    manifest_path = root / WORKSPACE_FILE
    if not manifest_path.is_file():
        raise WorkspaceError(
            f"No workspace manifest found at {manifest_path}. Declare the "
            "workspace members in .specify/workspace.yml (see the multi-repo "
            "extension README) and try again."
        )

    try:
        data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise WorkspaceError(f"Cannot read workspace manifest {manifest_path}: {exc}") from exc

    if not isinstance(data, dict):
        raise WorkspaceError(
            f"Workspace manifest {manifest_path} must be a YAML mapping, got "
            f"{type(data).__name__}."
        )

    if str(data.get("schema_version")) != WORKSPACE_SCHEMA_VERSION:
        raise WorkspaceError(
            f"Workspace manifest {manifest_path}: schema_version must be "
            f"{WORKSPACE_SCHEMA_VERSION!r} (quoted), got "
            f"{data.get('schema_version')!r}."
        )

    workspace_node = data.get("workspace")
    if not isinstance(workspace_node, dict):
        raise WorkspaceError(
            f"Workspace manifest {manifest_path}: 'workspace' must be a mapping."
        )

    unknown_workspace_keys = set(workspace_node) - _ALLOWED_WORKSPACE_KEYS
    if unknown_workspace_keys:
        raise WorkspaceError(
            f"Workspace manifest {manifest_path}: unknown 'workspace' keys: "
            f"{sorted(unknown_workspace_keys)}. Allowed: "
            f"{sorted(_ALLOWED_WORKSPACE_KEYS)}."
        )

    workspace_id = workspace_node.get("id")
    if not isinstance(workspace_id, str) or not workspace_id.strip():
        raise WorkspaceError(
            f"Workspace manifest {manifest_path}: 'workspace.id' is required "
            "and must be a non-empty string."
        )

    members_node = data.get("members")
    if not isinstance(members_node, list) or not members_node:
        raise WorkspaceError(
            f"Workspace manifest {manifest_path}: 'members' must be a "
            "non-empty list."
        )

    members: list[Member] = []
    seen_ids: set[str] = set()
    for index, entry in enumerate(members_node):
        if not isinstance(entry, dict):
            raise WorkspaceError(
                f"Workspace manifest {manifest_path}: members[{index}] must "
                "be a mapping."
            )
        unknown_keys = set(entry) - _ALLOWED_MEMBER_KEYS
        if unknown_keys:
            raise WorkspaceError(
                f"Workspace manifest {manifest_path}: members[{index}] has "
                f"unknown keys: {sorted(unknown_keys)}. Allowed: "
                f"{sorted(_ALLOWED_MEMBER_KEYS)}."
            )

        member_id = entry.get("id")
        if not isinstance(member_id, str) or not _MEMBER_ID_RE.match(member_id):
            raise WorkspaceError(
                f"Workspace manifest {manifest_path}: members[{index}].id "
                f"must match {_MEMBER_ID_RE.pattern!r}, got {member_id!r}."
            )
        if member_id in seen_ids:
            raise WorkspaceError(
                f"Workspace manifest {manifest_path}: duplicate member id "
                f"{member_id!r}."
            )
        seen_ids.add(member_id)

        raw_path = entry.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise WorkspaceError(
                f"Workspace manifest {manifest_path}: member {member_id!r} "
                "requires a non-empty 'path'."
            )
        path = Path(raw_path)
        if not path.is_absolute():
            path = root / path
        # resolve() without strict: tolerates not-yet-cloned members at parse
        # time (filesystem validation happens separately in the step).
        path = path.resolve()

        integration = entry.get("integration")
        if integration is not None and (
            not isinstance(integration, str) or not integration.strip()
        ):
            raise WorkspaceError(
                f"Workspace manifest {manifest_path}: member {member_id!r} "
                "'integration' must be a non-empty string or omitted."
            )

        tasks_file = entry.get("tasks_file")
        if tasks_file is None:
            tasks_file = f"tasks/{member_id}.md"
        elif not isinstance(tasks_file, str) or not tasks_file.strip():
            raise WorkspaceError(
                f"Workspace manifest {manifest_path}: member {member_id!r} "
                "'tasks_file' must be a non-empty string or omitted."
            )

        depends_node = entry.get("depends_on")
        if depends_node is None:
            depends_on: tuple[str, ...] = ()
        elif isinstance(depends_node, list) and all(
            isinstance(dep, str) and dep.strip() for dep in depends_node
        ):
            depends_on = tuple(dep.strip() for dep in depends_node)
        else:
            raise WorkspaceError(
                f"Workspace manifest {manifest_path}: member {member_id!r} "
                "'depends_on' must be a list of member ids or omitted."
            )

        domain_node = entry.get("domain")
        if domain_node is None:
            domain: str | None = None
        elif isinstance(domain_node, str) and domain_node.strip():
            domain = domain_node.strip()
        else:
            raise WorkspaceError(
                f"Workspace manifest {manifest_path}: member {member_id!r} "
                "'domain' must be a non-empty string or omitted."
            )

        communicates_node = entry.get("communicates_with")
        if communicates_node is None:
            communicates_with: tuple[str, ...] = ()
        elif isinstance(communicates_node, list) and all(
            isinstance(peer, str) and peer.strip() for peer in communicates_node
        ):
            communicates_with = tuple(
                dict.fromkeys(peer.strip() for peer in communicates_node)
            )
        else:
            raise WorkspaceError(
                f"Workspace manifest {manifest_path}: member {member_id!r} "
                "'communicates_with' must be a list of member ids or omitted."
            )

        constitution_raw = entry.get("constitution")
        if constitution_raw is None:
            constitution: Path | None = None
        elif isinstance(constitution_raw, str) and constitution_raw.strip():
            constitution = Path(constitution_raw.strip())
            if not constitution.is_absolute():
                constitution = root / constitution
            constitution = constitution.resolve()
        else:
            raise WorkspaceError(
                f"Workspace manifest {manifest_path}: member {member_id!r} "
                "'constitution' must be a non-empty path (relative to the "
                "workspace root) or omitted."
            )

        members.append(
            Member(
                id=member_id,
                path=path,
                integration=integration.strip() if integration else None,
                tasks_file=tasks_file,
                constitution=constitution,
                depends_on=depends_on,
                domain=domain,
                communicates_with=communicates_with,
            )
        )

    _validate_graph(manifest_path, members)

    return Workspace(
        id=workspace_id.strip(),
        name=str(workspace_node.get("name", workspace_id)),
        root=root,
        members=tuple(members),
    )


def _validate_graph(manifest_path: Path, members: list[Member]) -> None:
    """Reject unknown/self references, and cycles in ``depends_on``.

    ``communicates_with`` is checked for unknown and self references only:
    communication may be bidirectional, so cycles there are legitimate.
    """
    known = {member.id for member in members}
    for member in members:
        for peer in member.communicates_with:
            if peer == member.id:
                raise WorkspaceError(
                    f"Workspace manifest {manifest_path}: member {member.id!r} "
                    "cannot communicate with itself."
                )
            if peer not in known:
                raise WorkspaceError(
                    f"Workspace manifest {manifest_path}: member {member.id!r} "
                    f"communicates with unknown member {peer!r}. Known "
                    f"members: {sorted(known)}."
                )
        for dep in member.depends_on:
            if dep == member.id:
                raise WorkspaceError(
                    f"Workspace manifest {manifest_path}: member {member.id!r} "
                    "cannot depend on itself."
                )
            if dep not in known:
                raise WorkspaceError(
                    f"Workspace manifest {manifest_path}: member {member.id!r} "
                    f"depends on unknown member {dep!r}. Known members: "
                    f"{sorted(known)}."
                )

    cycle = _find_cycle(members)
    if cycle:
        raise WorkspaceError(
            f"Workspace manifest {manifest_path}: dependency cycle detected: "
            f"{' -> '.join([*cycle, cycle[0]])}."
        )


def _find_cycle(members: list[Member]) -> list[str] | None:
    """Return one dependency cycle as a list of member ids, or ``None``."""
    graph = {member.id: member.depends_on for member in members}

    state: dict[str, str] = {}  # id -> "visiting" | "done"
    stack: list[str] = []

    def visit(node: str) -> list[str] | None:
        state[node] = "visiting"
        stack.append(node)
        for dep in graph[node]:
            if state.get(dep) == "visiting":
                start = stack.index(dep)
                return stack[start:]
            if state.get(dep) is None:
                found = visit(dep)
                if found:
                    return found
        stack.pop()
        state[node] = "done"
        return None

    for member_id in sorted(graph):
        if state.get(member_id) is None:
            found = visit(member_id)
            if found:
                return found
    return None


def topological_waves(member_ids: list[str], graph: dict[str, tuple[str, ...]]) -> list[list[str]]:
    """Group ids into dependency waves using Kahn's algorithm.

    ``graph`` maps member id -> direct dependencies (which must have been
    validated already: known ids, no cycles). Only the ids listed in
    ``member_ids`` participate; dependencies outside that set (for example,
    a member not part of this feature) are ignored, which lets a feature
    dispatch a subset of the workspace.

    Within a wave, ids are sorted for deterministic dispatch order. Raises
    :class:`WorkspaceError` if the subgraph contains a cycle or unknown ids
    (defensive; ``load_workspace`` already rejects these for the full graph).
    """
    participants = list(dict.fromkeys(member_ids))
    participant_set = set(participants)

    unknown = [dep for mid in participants for dep in graph.get(mid, ()) if dep not in graph]
    if unknown:
        raise WorkspaceError(
            f"topological_waves: unknown member ids {sorted(set(unknown))}."
        )

    # Restrict edges to the participating subgraph.
    in_degree: dict[str, int] = {}
    dependents: dict[str, list[str]] = {mid: [] for mid in participants}
    for mid in participants:
        deps = [dep for dep in graph.get(mid, ()) if dep in participant_set]
        in_degree[mid] = len(deps)
        for dep in deps:
            dependents[dep].append(mid)

    waves: list[list[str]] = []
    ready = sorted(mid for mid, degree in in_degree.items() if degree == 0)
    emitted = 0
    while ready:
        waves.append(ready)
        emitted += len(ready)
        next_ready: list[str] = []
        for mid in ready:
            for dependent in dependents[mid]:
                in_degree[dependent] -= 1
                if in_degree[dependent] == 0:
                    next_ready.append(dependent)
        ready = sorted(next_ready)

    if emitted != len(participants):
        stuck = sorted(mid for mid, degree in in_degree.items() if degree > 0)
        raise WorkspaceError(
            f"topological_waves: dependency cycle among {stuck}."
        )

    return waves


def workspace_waves(workspace: Workspace, participant_ids: list[str]) -> list[list[str]]:
    """Convenience wrapper: waves over a workspace's members."""
    graph = {member.id: member.depends_on for member in workspace.members}
    return topological_waves(participant_ids, graph)


def topology_mermaid(workspace: Workspace) -> str:
    """Render the workspace as a Mermaid flowchart.

    Members are grouped into one subgraph per ``domain`` (members without a
    domain stay at the top level). Execution edges (``depends_on``) are drawn
    thick from the dependency to its dependent, in dispatch order;
    communication edges (``communicates_with``) are dotted, and a pair of
    members listing each other is drawn once as a bidirectional edge.
    """

    def node(member_id: str) -> str:
        return "m_" + member_id.replace("-", "_")

    lines = ["flowchart LR"]
    domains: dict[str, list[Member]] = {}
    for member in workspace.members:
        if member.domain is None:
            lines.append(f'  {node(member.id)}["{member.id}"]')
        else:
            domains.setdefault(member.domain, []).append(member)
    for index, (domain, domain_members) in enumerate(domains.items()):
        label = domain.replace('"', "'")
        lines.append(f'  subgraph d_{index}["{label}"]')
        for member in domain_members:
            lines.append(f'    {node(member.id)}["{member.id}"]')
        lines.append("  end")

    for member in workspace.members:
        for dep in member.depends_on:
            lines.append(f"  {node(dep)} ==> {node(member.id)}")

    # A producer/consumer pair already linked by depends_on is drawn once, as
    # the execution edge — the dotted communication edge would be a duplicate.
    execution_links = {
        frozenset((dep, member.id))
        for member in workspace.members
        for dep in member.depends_on
    }
    talks = {(member.id, peer) for member in workspace.members for peer in member.communicates_with}
    drawn: set[tuple[str, str]] = set()
    for member in workspace.members:
        for peer in member.communicates_with:
            if (member.id, peer) in drawn or frozenset((member.id, peer)) in execution_links:
                drawn.add((member.id, peer))
                continue
            if (peer, member.id) in talks:
                lines.append(f"  {node(member.id)} <-.-> {node(peer)}")
                drawn.add((peer, member.id))
            else:
                lines.append(f"  {node(member.id)} -.-> {node(peer)}")
            drawn.add((member.id, peer))

    return "\n".join(lines) + "\n"


def read_feature_directory(project_root: Path) -> str:
    """Read the active feature directory from the central project's state.

    Mirrors the priority the core scripts use for the feature axis:
    ``SPECIFY_FEATURE_DIRECTORY`` wins over ``.specify/feature.json``. No
    filesystem validation here — callers validate the directory exists.

    Raises :class:`WorkspaceError` when neither source is available.
    """
    import os

    env_value = os.environ.get("SPECIFY_FEATURE_DIRECTORY", "").strip()
    if env_value:
        return env_value

    feature_json = Path(project_root) / ".specify" / "feature.json"
    if feature_json.is_file():
        import json

        try:
            data = json.loads(feature_json.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            data = None
        value = data.get("feature_directory") if isinstance(data, dict) else None
        if isinstance(value, str) and value.strip():
            return value

    raise WorkspaceError(
        "No active feature directory. Pass the step's 'feature' setting, set "
        "SPECIFY_FEATURE_DIRECTORY, or run a specify command in the central "
        "repository to record .specify/feature.json."
    )
