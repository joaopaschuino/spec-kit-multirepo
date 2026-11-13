"""Workspace bootstrap: clone missing member repositories from their ``url``.

Lets a teammate clone only the central specs repository and materialize the
rest of the workspace from the manifest: every member whose ``path`` does not
exist and that declares a ``url`` is cloned with ``git clone <url> <path>``
(parent directories created). Members already present are left untouched;
absent members without a ``url`` are reported and skipped.

Members cloned into a subdirectory of the central repository are separate
git repositories, so their paths are added to the central ``.gitignore`` —
otherwise ``git add`` in the central repo would record them as embedded
repositories. Members without a ``url`` (mono-repo stacks tracked by the
central repository itself) are never ignored.

Pure module (no ``specify_cli`` imports) so it can be unit-tested directly.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any

from .workspace import Member, Workspace

GITIGNORE_HEADER = "# multi-repo: member repositories cloned by the bootstrap"


class BootstrapError(RuntimeError):
    """A member clone failed; ``result`` holds the partial bootstrap report."""

    def __init__(self, message: str, result: dict[str, Any]):
        super().__init__(message)
        self.result = result


def bootstrap_workspace(workspace: Workspace, *, dry_run: bool = False) -> dict[str, Any]:
    """Clone every absent member that declares a ``url``.

    Returns a report: ``members`` (one entry per declared member, manifest
    order, with ``status`` ``present`` / ``cloned`` / ``would-clone`` /
    ``missing-url``), the ``cloned``, ``present`` and ``missing`` id lists,
    and ``gitignore`` — the entries added to (or, on a dry run, that would
    be added to) the central ``.gitignore``. Raises :class:`BootstrapError`
    on the first failed clone; members cloned before it are kept and
    git-ignored.
    """
    entries: dict[str, dict[str, Any]] = {}
    to_clone: list[Member] = []
    for member in workspace.members:
        entry: dict[str, Any] = {"repo": member.id, "path": str(member.path)}
        if member.url:
            entry["url"] = member.url
        if member.path.exists():
            entry["status"] = "present"
        elif member.url:
            entry["status"] = "would-clone" if dry_run else "pending"
            to_clone.append(member)
        else:
            entry["status"] = "missing-url"
            entry["reason"] = (
                f"member path not found: {member.path}, and the manifest "
                "declares no 'url' to clone it from"
            )
        entries[member.id] = entry

    def report(gitignore: list[str]) -> dict[str, Any]:
        for member in workspace.members:
            entry = entries[member.id]
            if entry["status"] in ("present", "cloned"):
                entry["initialized"] = (member.path / ".specify").is_dir()
        return {
            "members": [entries[m.id] for m in workspace.members],
            "cloned": [m.id for m in workspace.members if entries[m.id]["status"] == "cloned"],
            "present": [m.id for m in workspace.members if entries[m.id]["status"] == "present"],
            "missing": [m.id for m in workspace.members if entries[m.id]["status"] == "missing-url"],
            "gitignore": gitignore,
        }

    if dry_run:
        ignore_members = [m for m in workspace.members if m.url]
        return report(ensure_gitignored(workspace.root, ignore_members, write=False))

    # Shallow paths first, so a member nested inside another member's path
    # is cloned after its parent instead of blocking it.
    failure: str | None = None
    for member in sorted(to_clone, key=lambda m: len(m.path.parts)):
        error = _clone(member, workspace.root)
        if error:
            entries[member.id]["status"] = "failed"
            entries[member.id]["reason"] = error
            failure = f"Bootstrap of member {member.id!r} failed: {error}"
            break
        entries[member.id]["status"] = "cloned"
    for entry in entries.values():
        if entry["status"] == "pending":
            entry["status"] = "not-attempted"

    ignore_members = [m for m in workspace.members if m.url and m.path.exists()]
    gitignore = ensure_gitignored(workspace.root, ignore_members)
    result = report(gitignore)
    if failure:
        result["failed"] = [m.id for m in workspace.members if entries[m.id]["status"] == "failed"]
        raise BootstrapError(failure, result)
    return result


def _clone(member: Member, root: Path) -> str | None:
    """Run ``git clone``; return an error message, or ``None`` on success."""
    assert member.url is not None
    try:
        member.path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return f"cannot create {member.path.parent}: {exc}"
    # Captured output means a credential prompt would be invisible: fail fast
    # instead (credential helpers and SSH agents still work). SSH ignores
    # GIT_TERMINAL_PROMPT and reads passphrases / host-key confirmations
    # from the controlling terminal, so the clone runs without one.
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "SSH_ASKPASS_REQUIRE": "never"}
    try:
        completed = subprocess.run(
            ["git", "clone", "--", member.url, str(member.path)],
            cwd=root,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            start_new_session=True,
        )
    except OSError as exc:
        return f"cannot run git: {exc}"
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        detail = "\n".join(detail.splitlines()[-5:])
        return (
            f"'git clone {member.url} {member.path}' exited with "
            f"{completed.returncode}" + (f": {detail}" if detail else "")
        )
    return None


def ensure_gitignored(root: Path, members: list[Member], *, write: bool = True) -> list[str]:
    """Git-ignore the given members' paths that lie inside ``root``.

    Returns the entries (``/<relative-path>/``) added — or, with
    ``write=False``, that would be added. Paths already ignored (by any
    pattern git honours, or a matching ``.gitignore`` line outside a git
    work tree) are skipped.
    """
    root = Path(root).resolve()
    gitignore = root / ".gitignore"
    existing = gitignore.read_text(encoding="utf-8") if gitignore.is_file() else ""

    added: list[str] = []
    for member in members:
        try:
            relative = member.path.resolve().relative_to(root)
        except ValueError:
            continue  # sibling checkout: outside the central repository
        if not relative.parts:
            continue
        rel = relative.as_posix()
        entry = f"/{_escape_gitignore(rel)}/"
        if entry in added or _is_ignored(root, rel, existing):
            continue
        added.append(entry)

    if added and write:
        lines = existing
        if lines and not lines.endswith("\n"):
            lines += "\n"
        if GITIGNORE_HEADER not in lines:
            lines += ("\n" if lines else "") + GITIGNORE_HEADER + "\n"
        lines += "".join(f"{entry}\n" for entry in added)
        gitignore.write_text(lines, encoding="utf-8")
    return added


def _escape_gitignore(path: str) -> str:
    """Escape gitignore glob characters so ``path`` matches literally."""
    return re.sub(r"([\\*?\[])", r"\\\1", path)


def _is_ignored(root: Path, rel: str, gitignore_text: str) -> bool:
    try:
        completed = subprocess.run(
            ["git", "check-ignore", "-q", "--", rel],
            cwd=root,
            capture_output=True,
            text=True,
        )
    except OSError:
        completed = None
    if completed is not None and completed.returncode in (0, 1):
        return completed.returncode == 0

    # Not a git work tree: match plain (glob-free) .gitignore lines.
    for line in gitignore_text.splitlines():
        pattern = line.strip()
        if not pattern or pattern.startswith(("#", "!")) or re.search(r"(?<!\\)[*?\[]", pattern):
            continue
        pattern = re.sub(r"\\(.)", r"\1", pattern).strip("/")
        if rel == pattern or rel.startswith(pattern + "/"):
            return True
    return False


def origin_url(path: Path) -> str | None:
    """Return the ``origin`` remote of the repository rooted at ``path``.

    ``None`` when ``path`` is not the top level of its own git repository —
    in particular for a mono-repo member subdirectory, whose ``origin`` is
    the central repository's and must not become the member's ``url``.
    """
    path = Path(path)
    if not path.is_dir():
        return None
    try:
        toplevel = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=path,
            capture_output=True,
            text=True,
        )
        if toplevel.returncode != 0 or Path(toplevel.stdout.strip()).resolve() != path.resolve():
            return None
        remote = subprocess.run(
            # The configured value, not ``remote get-url``: url.insteadOf
            # rewrites are local to this machine, not the teammate's.
            ["git", "config", "--get", "remote.origin.url"],
            cwd=path,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    url = remote.stdout.strip()
    if remote.returncode != 0 or not url:
        return None
    # A relative local remote is relative to the member; the bootstrap
    # resolves urls against the central root, so make it absolute.
    if not _URL_SCHEME_RE.match(url) and not _SCP_LIKE_RE.match(url) and not Path(url).is_absolute():
        url = str((path / url).resolve())
    return url


_URL_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
#: ``[user@]host:path`` — git's scp-like syntax (no slash before the colon).
_SCP_LIKE_RE = re.compile(r"^[^/]+:")
