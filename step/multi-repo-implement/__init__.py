"""Multi-repo implement step — dispatch feature implementation to members.

Runs in the **central specs repository** (``context.project_root``). For each
participating member of the workspace it dispatches the member's implement
command headlessly, with the member repository as the agent's working
directory and the central feature directory exported through
``SPECIFY_FEATURE_DIRECTORY``. Members run in dependency waves computed from
``.specify/workspace.yml``.

Configuration (all fields optional)::

    - id: implement
      type: multi-repo-implement
      feature: "specs/001-my-feature"   # default: active feature
      repos: [s3, lambda]               # default: members with a tasks file
      command: speckit.multi-repo.implement
      max_concurrency: 1                # per-wave parallelism (stream=False)
      dispatch_timeout: 3600            # seconds, parallel mode only
    skip_completed: true              # skip members with all tasks checked
    persist_feature: false            # write feature.json in members
    stop_on_failure: true             # halt before the next wave on failure
    dry_run: false                    # report member readiness, dispatch nothing
    missing: error                    # error | skip for locally-absent members

``dry_run: true`` turns the step into a read-only workspace report: every
declared member is classified (ready / not-cloned / not-initialized /
no-integration / unknown-integration / no-dispatch / constitution-missing),
participation and waves are computed, and nothing is dispatched. The
``multi-repo-check`` workflow wraps this mode as a workspace doctor.

``missing: skip`` supports teams where each member of the team has cloned
only part of the workspace: participants that are not cloned locally (or not
initialized) are recorded as ``status: missing`` in the results and the
ready members are dispatched. Configuration errors (no integration,
unknown integration, no dispatch support, missing constitution file) still
fail the run in both modes.

Members may also declare a shared ``constitution`` file (stored in the
central repository, e.g. ``constitutions/lambda.md``) in
``.specify/workspace.yml``; it is validated to exist before any dispatch and
its absolute path is appended to the dispatched command arguments so the
member's agent reads it as the member/type governance layer.

The step is stateless/thread-safe (shared instance): all per-run state comes
from ``config``/``context``, and process-wide environment variables are set
once before any dispatch and restored in a ``finally`` block.
"""

from __future__ import annotations

import os
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from specify_cli.workflows.base import StepBase, StepContext, StepResult, StepStatus
from specify_cli.workflows.expressions import evaluate_expression

from .workspace import (
    Member,
    Workspace,
    WorkspaceError,
    load_workspace,
    read_feature_directory,
    workspace_waves,
)

DEFAULT_COMMAND = "speckit.multi-repo.implement"

_CHECKBOX_RE = re.compile(r"^\s*[-*]\s+\[(?P<mark>[ xX])\]", re.MULTILINE)

_KNOWN_CONFIG_KEYS = {
    "id",
    "type",
    "feature",
    "repos",
    "command",
    "max_concurrency",
    "dispatch_timeout",
    "skip_completed",
    "persist_feature",
    "stop_on_failure",
    "dry_run",
    "missing",
    "model",
}

#: Statuses that mean "this teammate's machine lacks the member locally".
_LOCALLY_MISSING_STATUSES = ("not-cloned", "not-initialized")

#: Statuses that mean the workspace configuration itself is wrong.
_CONFIG_ERROR_STATUSES = (
    "no-integration",
    "unknown-integration",
    "no-dispatch",
    "constitution-missing",
)


class MultiRepoImplementStep(StepBase):
    """Dispatch a centrally-specified feature to workspace members."""

    type_key = "multi-repo-implement"

    # -- validation ---------------------------------------------------------

    def validate(self, config: dict[str, Any]) -> list[str]:
        errors = super().validate(config)
        step_id = repr(config.get("id", "?"))

        unknown = set(config) - _KNOWN_CONFIG_KEYS
        if unknown:
            errors.append(
                f"Multi-repo-implement step {step_id}: unknown fields: "
                f"{sorted(unknown)}. Allowed: {sorted(_KNOWN_CONFIG_KEYS - {'id', 'type'})}."
            )

        errors.extend(self._validate_string(config, "feature"))
        errors.extend(self._validate_string(config, "command"))
        errors.extend(self._validate_string(config, "model"))

        repos = config.get("repos")
        if repos is not None and (
            not isinstance(repos, list)
            or not all(isinstance(item, str) and item.strip() for item in repos)
        ):
            errors.append(
                f"Multi-repo-implement step {step_id}: 'repos' must be a list "
                "of member ids."
            )

        for field in ("max_concurrency", "dispatch_timeout"):
            value = config.get(field)
            if value is None:
                continue
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or value <= 0
            ):
                errors.append(
                    f"Multi-repo-implement step {step_id}: '{field}' must be "
                    f"a positive number, got {value!r}."
                )

        for field in ("skip_completed", "persist_feature", "stop_on_failure"):
            value = config.get(field)
            if value is not None and not isinstance(value, bool):
                errors.append(
                    f"Multi-repo-implement step {step_id}: '{field}' must be "
                    f"true or false, got {value!r}."
                )

        missing_value = config.get("missing")
        if missing_value is not None and (
            not isinstance(missing_value, str)
            or ("{{" not in missing_value and missing_value not in ("error", "skip"))
        ):
            errors.append(
                f"Multi-repo-implement step {step_id}: 'missing' must be "
                f"'error' or 'skip', got {missing_value!r}."
            )

        dry_run_value = config.get("dry_run")
        if dry_run_value is not None and not isinstance(dry_run_value, (bool, str)):
            errors.append(
                f"Multi-repo-implement step {step_id}: 'dry_run' must be "
                f"true or false, got {dry_run_value!r}."
            )

        return errors

    @staticmethod
    def _validate_string(config: dict[str, Any], field: str) -> list[str]:
        value = config.get(field)
        if value is not None and not isinstance(value, str):
            return [
                f"Multi-repo-implement step {repr(config.get('id', '?'))}: "
                f"'{field}' must be a string, got {type(value).__name__}."
            ]
        return []

    # -- execution ----------------------------------------------------------

    def execute(self, config: dict[str, Any], context: StepContext) -> StepResult:
        settings = self._resolve_settings(config, context)

        output: dict[str, Any] = {"command": settings["command"]}

        # 1. Workspace manifest.
        try:
            workspace = load_workspace(Path(context.project_root or os.getcwd()))
        except WorkspaceError as exc:
            return StepResult(status=StepStatus.FAILED, output=output, error=str(exc))
        output["workspace"] = workspace.id

        # 2. Central feature directory. Optional for dry runs: a workspace
        # check without an active feature reports the whole workspace.
        feature_raw = settings["feature"] or read_feature_directory_safe(workspace.root)
        feature_dir: Path | None = None
        if feature_raw:
            feature_dir = Path(feature_raw)
            if not feature_dir.is_absolute():
                feature_dir = workspace.root / feature_dir
            feature_dir = feature_dir.resolve()
            if not feature_dir.is_dir():
                return StepResult(
                    status=StepStatus.FAILED,
                    output=output,
                    error=f"Feature directory not found: {feature_dir}",
                )
            output["feature"] = str(feature_dir)
        elif not settings["dry_run"]:
            return StepResult(
                status=StepStatus.FAILED,
                output=output,
                error=(
                    "No feature directory. Set the step's 'feature' setting "
                    "or record an active feature in the central repository."
                ),
            )

        # 3. Participating members (all declared members on a feature-less
        # dry run — the workspace-level readiness report).
        participants = self._participants(workspace, settings, feature_dir)
        if not participants and not settings["dry_run"]:
            return StepResult(
                status=StepStatus.FAILED,
                output=output,
                error=(
                    "No workspace members participate in this feature. Give "
                    "the step's 'repos' setting a member id list, or create "
                    "each member's task file under the feature directory "
                    "(run /speckit.multi-repo.tasks in the central repository)."
                ),
            )

        # 4. Member readiness classification (filesystem + integration).
        classify_scope = list(workspace.members) if settings["dry_run"] else participants
        report = self._classify_members(classify_scope)
        participant_ids = {member.id for member in participants}
        output["members"] = []
        for member in classify_scope:
            classified = report[member.id]
            entry: dict[str, Any] = {
                "repo": member.id,
                "path": str(member.path),
                "tasks_file": member.tasks_file,
                "status": classified["status"],
            }
            if classified["reason"]:
                entry["reason"] = classified["reason"]
            if classified["integration"]:
                entry["integration"] = classified["integration"]
            if member.constitution:
                entry["constitution"] = str(member.constitution)
            if settings["dry_run"]:
                entry["participant"] = member.id in participant_ids
            output["members"].append(entry)

        config_errors = {
            member.id: report[member.id]["reason"]
            for member in classify_scope
            if report[member.id]["status"] in _CONFIG_ERROR_STATUSES
        }
        locally_missing = [
            member
            for member in classify_scope
            if report[member.id]["status"] in _LOCALLY_MISSING_STATUSES
        ]

        # 5. Dry run: report readiness and waves, dispatch nothing.
        if settings["dry_run"]:
            ready_ids = [
                member.id
                for member in participants
                if report[member.id]["status"] == "ready"
            ]
            output["dry_run"] = True
            output["waves"] = workspace_waves(workspace, ready_ids)
            output["results"] = []
            output["failed"] = []
            output["missing"] = [member.id for member in locally_missing]
            return StepResult(status=StepStatus.COMPLETED, output=output)

        # Configuration errors abort in every mode: they are manifest/setup
        # mistakes, not a teammate's missing clone.
        if config_errors:
            details = "; ".join(f"{mid}: {err}" for mid, err in sorted(config_errors.items()))
            return StepResult(
                status=StepStatus.FAILED,
                output=output,
                error=f"Member validation failed. {details}",
            )

        # Locally-absent participants: the default aborts before any
        # dispatch; "skip" records them and dispatches the ready members.
        if locally_missing and settings["missing"] != "skip":
            details = "; ".join(
                f"{member.id}: {report[member.id]['reason']}" for member in locally_missing
            )
            return StepResult(
                status=StepStatus.FAILED,
                output=output,
                error=f"Member validation failed. {details}",
            )

        # 6. Dependency waves over the ready members.
        ready = [
            member for member in participants if report[member.id]["status"] == "ready"
        ]
        integration_keys = {
            member.id: report[member.id]["integration"] for member in ready
        }
        waves = workspace_waves(workspace, [member.id for member in ready])
        output["waves"] = waves
        output["missing"] = [member.id for member in locally_missing]

        # 7. Dispatch, wave by wave, with the feature axis exported.
        results: list[dict[str, Any]] = []
        failed: list[str] = []
        stopped_after_wave: int | None = None
        env_token = self._apply_feature_env(feature_dir, settings["persist_feature"])
        try:
            for wave_index, wave in enumerate(waves):
                wave_members = [m for m in ready if m.id in wave]
                for member, result in self._dispatch_wave(
                    wave_members,
                    wave_index,
                    settings,
                    feature_dir,
                    integration_keys,
                ):
                    results.append(result)
                    if result["status"] == "failed":
                        failed.append(member.id)
                if failed and settings["stop_on_failure"]:
                    stopped_after_wave = wave_index
                    break
        finally:
            self._restore_feature_env(env_token)

        for member in locally_missing:
            results.append(
                {
                    "repo": member.id,
                    "wave": None,
                    "tasks_file": member.tasks_file,
                    "integration": None,
                    "constitution": str(member.constitution) if member.constitution else None,
                    "status": "missing",
                    "reason": report[member.id]["reason"],
                    "exit_code": None,
                }
            )

        output["results"] = results
        output["failed"] = failed
        output["stopped_after_wave"] = stopped_after_wave

        if failed:
            summary = ", ".join(
                f"{r['repo']} (exit {r['exit_code']})" for r in results if r["status"] == "failed"
            )
            return StepResult(
                status=StepStatus.FAILED,
                output=output,
                error=f"{len(failed)} member dispatch(es) failed: {summary}.",
            )
        return StepResult(status=StepStatus.COMPLETED, output=output)

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _resolve_settings(config: dict[str, Any], context: StepContext) -> dict[str, Any]:
        def resolve(value: Any) -> Any:
            if isinstance(value, str) and "{{" in value:
                return evaluate_expression(value, context)
            return value

        def resolve_bool(value: Any, default: bool) -> bool:
            resolved = resolve(value) if value is not None else None
            if isinstance(resolved, str):
                return resolved.strip().lower() in ("true", "1", "yes")
            if resolved is None:
                return default
            return bool(resolved)

        def resolve_int(value: Any, default: int) -> int:
            resolved = resolve(value) if value is not None else None
            try:
                coerced = int(resolved)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return default
            return coerced if coerced > 0 else default

        repos = resolve(config.get("repos"))
        if isinstance(repos, str) and repos.strip():
            # Tolerate a comma-separated expression result.
            repos = [item.strip() for item in repos.split(",") if item.strip()]
        if not isinstance(repos, list):
            repos = None

        missing_raw = resolve(config.get("missing"))
        missing = str(missing_raw).strip().lower() if missing_raw is not None else ""
        if missing not in ("error", "skip"):
            missing = "error"

        return {
            "feature": str(resolve(config.get("feature")) or "").strip() or None,
            "repos": repos,
            "command": str(resolve(config.get("command")) or "").strip() or DEFAULT_COMMAND,
            "model": str(resolve(config.get("model")) or "").strip() or None,
            "max_concurrency": resolve_int(config.get("max_concurrency"), 1),
            "dispatch_timeout": resolve_int(config.get("dispatch_timeout"), 3600),
            "skip_completed": resolve_bool(config.get("skip_completed"), True),
            "persist_feature": resolve_bool(config.get("persist_feature"), False),
            "stop_on_failure": resolve_bool(config.get("stop_on_failure"), True),
            "dry_run": resolve_bool(config.get("dry_run"), False),
            "missing": missing,
        }

    @staticmethod
    def _participants(
        workspace: Workspace,
        settings: dict[str, Any],
        feature_dir: Path | None,
    ) -> list[Member]:
        if settings["repos"] is not None:
            return [m for m in workspace.members if m.id in set(settings["repos"])]
        if feature_dir is None:
            # No active feature (feature-less dry run): report the whole
            # declared workspace.
            return list(workspace.members)
        # Auto-detection: a member participates when its task file exists in
        # the central feature directory.
        return [m for m in workspace.members if (feature_dir / m.tasks_file).is_file()]

    @staticmethod
    def _classify_members(
        members: list[Member],
    ) -> dict[str, dict[str, Any]]:
        """Classify each member's dispatch readiness.

        Returns ``{member_id: {"status", "integration", "reason"}}`` with
        statuses ``ready``, ``not-cloned``, ``not-initialized``,
        ``no-integration``, ``unknown-integration``, ``no-dispatch``, and
        ``constitution-missing``. ``ready`` entries carry the resolved
        integration key; every other entry carries an actionable reason.
        """
        from specify_cli.integration_state import (
            default_integration_key,
            try_read_integration_json,
        )
        from specify_cli.integrations import get_integration

        report: dict[str, dict[str, Any]] = {}
        for member in members:
            entry: dict[str, Any] = {"status": "ready", "integration": None, "reason": None}

            if not member.path.is_dir():
                entry["status"] = "not-cloned"
                entry["reason"] = f"member path not found: {member.path}"
                report[member.id] = entry
                continue
            if not (member.path / ".specify").is_dir():
                entry["status"] = "not-initialized"
                entry["reason"] = (
                    f"{member.path} is not a Spec Kit project (no .specify/); "
                    "run 'specify init' in the member repository first"
                )
                report[member.id] = entry
                continue
            if member.constitution is not None and not member.constitution.is_file():
                entry["status"] = "constitution-missing"
                entry["reason"] = (
                    f"workspace constitution file not found: {member.constitution} "
                    "(the member's 'constitution' in workspace.yml must point to "
                    "an existing file in the central repository)"
                )
                report[member.id] = entry
                continue

            key = member.integration
            if not key:
                state, _error = try_read_integration_json(member.path)
                key = default_integration_key(state) if state else None
            if not key:
                entry["status"] = "no-integration"
                entry["reason"] = (
                    "no integration configured; run 'specify init' in the "
                    "member repository or set 'integration' in workspace.yml"
                )
                report[member.id] = entry
                continue

            impl = get_integration(key)
            if impl is None:
                entry["status"] = "unknown-integration"
                entry["reason"] = f"unknown integration {key!r}"
                report[member.id] = entry
                continue
            if impl.build_exec_args("__speckit_multi_repo_probe__") is None:
                entry["status"] = "no-dispatch"
                entry["reason"] = (
                    f"integration {key!r} does not support non-interactive "
                    "CLI dispatch"
                )
                report[member.id] = entry
                continue

            entry["integration"] = key
            report[member.id] = entry
        return report

    def _dispatch_wave(
        self,
        wave_members: list[Member],
        wave_index: int,
        settings: dict[str, Any],
        feature_dir: Path,
        integration_keys: dict[str, str],
    ) -> list[tuple[Member, dict[str, Any]]]:
        concurrency = settings["max_concurrency"]
        if concurrency > 1 and len(wave_members) > 1:
            with ThreadPoolExecutor(max_workers=min(concurrency, len(wave_members))) as pool:
                futures = [
                    (
                        member,
                        pool.submit(
                            self._dispatch_member,
                            member,
                            wave_index,
                            settings,
                            feature_dir,
                            integration_keys,
                        ),
                    )
                    for member in wave_members
                ]
                return [(member, future.result()) for member, future in futures]
        return [
            (
                member,
                self._dispatch_member(
                    member, wave_index, settings, feature_dir, integration_keys
                ),
            )
            for member in wave_members
        ]

    def _dispatch_member(
        self,
        member: Member,
        wave_index: int,
        settings: dict[str, Any],
        feature_dir: Path,
        integration_keys: dict[str, str],
    ) -> dict[str, Any]:
        from specify_cli.integrations import get_integration

        key = integration_keys.get(member.id) or ""
        result: dict[str, Any] = {
            "repo": member.id,
            "wave": wave_index,
            "tasks_file": member.tasks_file,
            "integration": key or None,
            "constitution": str(member.constitution) if member.constitution else None,
            "status": "failed",
            "exit_code": None,
        }

        if settings["skip_completed"] and self._tasks_completed(feature_dir / member.tasks_file):
            result["status"] = "skipped"
            result["exit_code"] = 0
            return result

        impl = get_integration(key)
        if impl is None:
            result["error"] = "integration vanished between validation and dispatch"
            return result

        # The member's workspace constitution path rides in the command
        # arguments (never the environment): per-dispatch data stays safe
        # under concurrent waves, where process-wide env would race.
        args = member.tasks_file
        if member.constitution is not None:
            args = f"{args} {member.constitution}"

        # Sequential waves stream agent output live; parallel waves capture
        # (interleaved streams would be unreadable) under a timeout.
        stream = settings["max_concurrency"] <= 1
        try:
            dispatch = impl.dispatch_command(
                settings["command"],
                args=args,
                project_root=member.path,
                model=settings["model"],
                timeout=settings["dispatch_timeout"],
                stream=stream,
            )
        except Exception as exc:  # noqa: BLE001 -- aggregate per member
            result["error"] = f"{type(exc).__name__}: {exc}"
            return result

        exit_code = dispatch.get("exit_code")
        result["exit_code"] = exit_code
        if exit_code == 0:
            result["status"] = "completed"
        else:
            result["status"] = "failed"
            stderr = (dispatch.get("stderr") or "").strip()
            if stderr:
                result["error"] = stderr.splitlines()[-1]
        return result

    @staticmethod
    def _tasks_completed(tasks_path: Path) -> bool:
        """True when the member task file exists and every checkbox is checked."""
        try:
            text = tasks_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return False
        marks = _CHECKBOX_RE.findall(text)
        return bool(marks) and all(mark != " " for mark in marks)

    @staticmethod
    def _apply_feature_env(feature_dir: Path, persist_feature: bool) -> dict[str, str | None]:
        """Export the feature axis for dispatched agents; return restore token."""
        token = {
            "SPECIFY_FEATURE_DIRECTORY": os.environ.get("SPECIFY_FEATURE_DIRECTORY"),
            "SPECIFY_FEATURE_NO_PERSIST": os.environ.get("SPECIFY_FEATURE_NO_PERSIST"),
        }
        os.environ["SPECIFY_FEATURE_DIRECTORY"] = str(feature_dir)
        if persist_feature:
            os.environ.pop("SPECIFY_FEATURE_NO_PERSIST", None)
        else:
            os.environ["SPECIFY_FEATURE_NO_PERSIST"] = "1"
        return token

    @staticmethod
    def _restore_feature_env(token: dict[str, str | None]) -> None:
        for key, previous in token.items():
            if previous is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous


def read_feature_directory_safe(project_root: Path) -> str:
    """read_feature_directory that returns '' instead of raising."""
    try:
        return read_feature_directory(project_root)
    except WorkspaceError:
        return ""
