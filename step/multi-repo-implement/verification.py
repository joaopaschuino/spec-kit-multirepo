"""Objective verification for the multi-repo orchestrator.

A member task file may carry a ``## Verification`` section listing POSIX
commands the orchestrator runs in the member repository after the member's
agent finishes. One command per bullet, an optional label, and an optional
expected exit code (default ``0``)::

    ## Verification

    - lint: `npm run lint` → exit 0
    - test: `npm test`
    - e2e: `npx playwright test` → exit 0

The section is parsed mechanically; whether a member is done is decided by
these exit codes, not by the agent's own report. This module also owns the
files the verification produces under ``<feature>/verification/`` (per-member
log, retry feedback, run report) and the Status column of the orchestration
page (``<feature>/tasks.md``).
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

#: Verification artifacts live here, relative to the feature directory.
VERIFICATION_DIR = "verification"

#: Characters of each command's output kept in the step results (the full
#: log is written to ``verification/<member>.log``).
RESULT_LOG_TAIL = 2000

_SECTION_RE = re.compile(r"^##\s+Verification\s*$", re.IGNORECASE | re.MULTILINE)
_NEXT_HEADING_RE = re.compile(r"^#{1,2}\s", re.MULTILINE)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_FENCE_RE = re.compile(r"^\s*(```|~~~).*?^\s*\1\s*$", re.DOTALL | re.MULTILINE)
_COMMAND_LINE_RE = re.compile(
    r"^\s*[-*]\s+(?:(?P<name>[^`:\n]+?):\s*)?(?P<tick>`+)(?P<command>.+?)(?P=tick)(?P<rest>.*)$"
)
_EXPECTED_EXIT_RE = re.compile(r"\bexit(?:\s+code)?\s*[:=]?\s*(?P<code>\d+)", re.IGNORECASE)
_CHECKBOX_RE = re.compile(r"^\s*[-*]\s+\[(?P<mark>[ xX])\]", re.MULTILINE)
_FRONT_MATTER_RE = re.compile(r"\A---[ \t]*\r?\n(?P<body>.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)

#: Serializes read-modify-write of the orchestration page across the
#: threads of a parallel wave.
_STATUS_LOCK = threading.Lock()


@dataclass(frozen=True)
class VerificationCommand:
    """One objective check from a member's ``## Verification`` section."""

    name: str
    command: str
    expected_exit: int = 0


def parse_verification(text: str) -> list[VerificationCommand] | None:
    """Return the commands of the ``## Verification`` section.

    ``None`` means the task file has no such section; an empty list means
    the section exists but lists no command. HTML comments and fenced code
    blocks inside the section are ignored, so templates can carry examples.
    """
    match = _SECTION_RE.search(text)
    if match is None:
        return None
    body = text[match.end():]
    following = _NEXT_HEADING_RE.search(body)
    if following is not None:
        body = body[: following.start()]
    body = _FENCE_RE.sub("", _COMMENT_RE.sub("", body))

    commands: list[VerificationCommand] = []
    for line in body.splitlines():
        parsed = _COMMAND_LINE_RE.match(line)
        if parsed is None:
            continue
        command = parsed.group("command").strip()
        if not command:
            continue
        expected = _EXPECTED_EXIT_RE.search(parsed.group("rest"))
        name = (parsed.group("name") or "").strip() or f"check-{len(commands) + 1}"
        commands.append(
            VerificationCommand(
                name=name,
                command=command,
                expected_exit=int(expected.group("code")) if expected else 0,
            )
        )
    return commands


def has_task_checkboxes(text: str) -> bool:
    """True when the task file lists at least one ``- [ ]``/``- [x]`` task."""
    return _CHECKBOX_RE.search(text) is not None


def is_marked_ready(text: str) -> bool:
    """False when the task file's YAML front matter sets ``ready: false``."""
    match = _FRONT_MATTER_RE.match(text)
    if match is None:
        return True
    try:
        data = yaml.safe_load(match.group("body"))
    except yaml.YAMLError:
        return True
    if not isinstance(data, dict):
        return True
    ready = data.get("ready", True)
    if isinstance(ready, str):
        return ready.strip().lower() not in ("false", "no", "0")
    return ready is not False


def run_verification(
    commands: list[VerificationCommand],
    *,
    cwd: Path,
    timeout: float | None,
    log_path: Path,
    attempt: int,
    label: str,
) -> dict[str, Any]:
    """Run every command in a POSIX shell with *cwd* as working directory.

    All commands run (a failure does not short-circuit the rest), so retry
    feedback names every broken check at once. Each command is bounded by
    *timeout* seconds. Output is appended to *log_path* under a per-attempt
    header. Returns ``{"status": "verified" | "failed", "attempt", "label",
    "commands": [...]}``.
    """
    shell = shutil.which("sh")
    results: list[dict[str, Any]] = []
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log:
        log.write(f"### attempt {attempt} ({label})\n")
        for check in commands:
            started = time.monotonic()
            if shell is None:
                exit_code, output, timed_out = None, "no POSIX shell (sh) found on PATH", False
            else:
                exit_code, output, timed_out = _run_bounded(
                    [shell, "-c", check.command], cwd=cwd, timeout=timeout
                )
            duration = round(time.monotonic() - started, 3)
            passed = not timed_out and exit_code == check.expected_exit
            if timed_out:
                output = f"{output}\n[timed out after {timeout}s]".lstrip("\n")
            log.write(
                f"=== {check.name}: {check.command} "
                f"(exit {exit_code}, expected {check.expected_exit}, "
                f"{'passed' if passed else 'failed'}, {duration}s)\n"
            )
            log.write(output if output.endswith("\n") or not output else output + "\n")
            results.append(
                {
                    "name": check.name,
                    "command": check.command,
                    "expected_exit": check.expected_exit,
                    "exit_code": exit_code,
                    "status": "passed" if passed else "failed",
                    "timed_out": timed_out,
                    "duration": duration,
                    "log_tail": output[-RESULT_LOG_TAIL:],
                }
            )
    return {
        "status": "verified" if all(r["status"] == "passed" for r in results) else "failed",
        "attempt": attempt,
        "label": label,
        "commands": results,
    }


def _run_bounded(
    argv: list[str], *, cwd: Path, timeout: float | None
) -> tuple[int | None, str, bool]:
    """Run *argv*, merging stdout/stderr; kill its process tree on timeout."""
    popen_kwargs: dict[str, Any] = {}
    if os.name == "posix":
        popen_kwargs["start_new_session"] = True
    else:  # pragma: no cover - exercised on Windows only
        popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        proc = subprocess.Popen(
            argv,
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            **popen_kwargs,
        )
    except OSError as exc:
        return None, f"cannot start command: {exc}", False
    try:
        output, _ = proc.communicate(timeout=timeout)
        return proc.returncode, output or "", False
    except subprocess.TimeoutExpired:
        kill_process_tree(proc)
        output, _ = proc.communicate()
        return proc.returncode, output or "", True
    except BaseException:
        kill_process_tree(proc)
        proc.wait()
        raise


def kill_process_tree(proc: subprocess.Popen) -> None:
    """Kill every descendant of *proc*, then *proc* itself.

    *proc* is frozen first so it cannot spawn more children; descendants are
    then found by walking the parent/child links (so they are reached even
    when they moved to another process group or session), frozen as they are
    found, and killed once the tree is stable. If *proc* leads its own
    process group, the rest of that group is killed too. Descendants already
    re-parented to init (double-forked daemons) are out of reach.
    """
    if os.name == "posix":
        frozen: list[int] = []
        _signal(proc.pid, signal.SIGSTOP)
        for _ in range(10):
            new = [pid for pid in _descendants(proc.pid) if pid not in frozen]
            if not new:
                break
            for pid in new:
                _signal(pid, signal.SIGSTOP)
            frozen.extend(new)
        for pid in reversed(frozen):
            _signal(pid, signal.SIGKILL)
        try:
            leads_group = os.getpgid(proc.pid) == proc.pid
        except (ProcessLookupError, PermissionError):
            leads_group = False
        if leads_group:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
    try:
        proc.kill()
    except ProcessLookupError:
        pass


def _signal(pid: int, sig: int) -> None:
    try:
        os.kill(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _descendants(root: int) -> list[int]:
    """PIDs below *root* in the process tree, parents before children."""
    children: dict[int, list[int]] = {}
    for pid, ppid in _process_table():
        children.setdefault(ppid, []).append(pid)
    found: list[int] = []
    queue = [root]
    while queue:
        for child in children.get(queue.pop(0), []):
            if child not in found and child != root:
                found.append(child)
                queue.append(child)
    return found


def _process_table() -> list[tuple[int, int]]:
    """``(pid, ppid)`` of every process: ``/proc`` on Linux, else ``ps``."""
    table: list[tuple[int, int]] = []
    proc_dir = Path("/proc")
    if (proc_dir / "self" / "stat").exists():
        for entry in proc_dir.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                stat = (entry / "stat").read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            fields = stat.rsplit(")", 1)[-1].split()
            if len(fields) >= 2 and fields[1].isdigit():
                table.append((int(entry.name), int(fields[1])))
        return table
    try:
        listing = subprocess.run(
            ["ps", "-A", "-o", "pid=", "-o", "ppid="],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return table
    for line in listing.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            table.append((int(parts[0]), int(parts[1])))
    return table


def write_feedback(
    feedback_path: Path,
    member_id: str,
    tasks_file: str,
    verification: dict[str, Any],
    log_path: Path,
) -> Path:
    """Write the retry feedback file handed to the member's next dispatch."""
    lines = [
        f"# Verification feedback — {member_id}",
        "",
        "The multi-repo orchestrator ran the objective checks from the",
        f"`## Verification` section of `{tasks_file}` in this repository after",
        f"the previous attempt ({verification['label']}), and some failed.",
        "Fix the implementation so every check exits with its expected code.",
        "Do not edit or weaken the `## Verification` section, and do not",
        "uncheck completed tasks.",
        "",
        f"Full output of every check: `{log_path}`.",
    ]
    for check in verification["commands"]:
        if check["status"] == "passed":
            continue
        reason = (
            "timed out"
            if check["timed_out"]
            else f"exit {check['exit_code']}, expected {check['expected_exit']}"
        )
        lines.extend(
            [
                "",
                f"## {check['name']} — {reason}",
                "",
                f"Command: `{check['command']}`",
                "",
                "```text",
                check["log_tail"].rstrip("\n").replace("```", "'''"),
                "```",
            ]
        )
    passed = [c["name"] for c in verification["commands"] if c["status"] == "passed"]
    if passed:
        lines.extend(["", f"Checks already passing: {', '.join(passed)}."])
    feedback_path.parent.mkdir(parents=True, exist_ok=True)
    feedback_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return feedback_path


def update_orchestration_status(tasks_md: Path, member_id: str, status: str) -> bool:
    """Set *member_id*'s Status cell in the orchestration page table.

    The table is the one whose header row names both a ``Member`` and a
    ``Status`` column (written by ``/speckit.multi-repo.tasks``). Only that
    cell changes; the rest of the file is preserved byte for byte. Returns
    ``True`` when a row was updated. Missing files, missing tables, and
    unknown members are a silent no-op: the page is a human-readable mirror,
    never a dispatch input.
    """
    with _STATUS_LOCK:
        try:
            text = tasks_md.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return False
        lines = text.splitlines(keepends=True)
        member_col = status_col = None
        in_table = False
        changed = False
        for index, line in enumerate(lines):
            stripped = line.strip()
            if not stripped.startswith("|"):
                member_col = status_col = None
                in_table = False
                continue
            cells = _split_row(stripped)
            if member_col is None:
                lowered = [cell.strip().lower() for cell in cells]
                if "member" in lowered and "status" in lowered:
                    member_col = lowered.index("member")
                    status_col = lowered.index("status")
                continue
            if not in_table:
                in_table = True  # separator row
                continue
            if len(cells) <= max(member_col, status_col):
                continue
            if cells[member_col].strip().strip("`").strip() != member_id:
                continue
            if cells[status_col].strip() == status:
                return False
            cells[status_col] = f" {status} "
            ending = line[len(line.rstrip("\r\n")):]
            indent = line[: len(line) - len(line.lstrip())]
            lines[index] = f"{indent}|{'|'.join(cells)}|{ending}"
            changed = True
            break
        if not changed:
            return False
        try:
            tasks_md.write_text("".join(lines), encoding="utf-8")
        except OSError:
            return False
        return True


def _split_row(row: str) -> list[str]:
    """Split a Markdown table row into raw cells (outer pipes dropped)."""
    inner = row
    if inner.startswith("|"):
        inner = inner[1:]
    if inner.endswith("|"):
        inner = inner[:-1]
    return inner.split("|")


def write_report(report_path: Path, feature_dir: Path, results: list[dict[str, Any]]) -> Path:
    """Write the run's verification report (shown at the review gate)."""
    lines = [
        f"# Verification report — {feature_dir.name}",
        "",
        "| Member | Dispatch | Verification | Attempts | Failed checks |",
        "|--------|----------|--------------|----------|---------------|",
    ]
    for result in results:
        verification = result.get("verification") or {}
        failed_checks = [
            c["name"] for c in verification.get("commands", []) if c["status"] != "passed"
        ]
        lines.append(
            "| {repo} | {status} | {verification} | {attempts} | {failed} |".format(
                repo=result["repo"],
                status=result["status"],
                verification=verification.get("status", "—"),
                attempts=result.get("attempts", 0),
                failed=", ".join(failed_checks) or "—",
            )
        )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report_path
