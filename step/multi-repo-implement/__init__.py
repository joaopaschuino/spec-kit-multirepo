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
    "model",
}


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

        # 2. Central feature directory.
        feature_raw = settings["feature"] or read_feature_directory_safe(workspace.root)
        if not feature_raw:
            return StepResult(
                status=StepStatus.FAILED,
                output=output,
                error=(
                    "No feature directory. Set the step's 'feature' setting "
                    "or record an active feature in the central repository."
                ),
            )
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

        # 3. Participating members.
        participants = self._participants(workspace, settings, feature_dir)
        if not participants:
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

        # 4. Member validation (filesystem + integration) before any dispatch.
        integration_keys, invalid = self._resolve_integrations(participants)
        if invalid:
            details = "; ".join(f"{mid}: {err}" for mid, err in sorted(invalid.items()))
            return StepResult(
                status=StepStatus.FAILED,
                output=output,
                error=f"Member validation failed. {details}",
            )

        # 5. Dependency waves.
        waves = workspace_waves(workspace, [member.id for member in participants])
        output["waves"] = waves

        # 6. Dispatch, wave by wave, with the feature axis exported.
        env_token = self._apply_feature_env(feature_dir, settings["persist_feature"])
        try:
            results: list[dict[str, Any]] = []
            failed: list[str] = []
            stopped_after_wave: int | None = None
            for wave_index, wave in enumerate(waves):
                wave_members = [m for m in participants if m.id in wave]
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
        }

    @staticmethod
    def _participants(
        workspace: Workspace, settings: dict[str, Any], feature_dir: Path
    ) -> list[Member]:
        if settings["repos"] is not None:
            return [m for m in workspace.members if m.id in set(settings["repos"])]
        # Auto-detection: a member participates when its task file exists in
        # the central feature directory.
        return [m for m in workspace.members if (feature_dir / m.tasks_file).is_file()]

    @staticmethod
    def _resolve_integrations(
        participants: list[Member],
    ) -> tuple[dict[str, str], dict[str, str]]:
        """Validate each participant.

        Returns ``(integration_keys, errors)``: ``integration_keys`` maps
        member id -> integration key for every dispatchable member;
        ``errors`` maps member id -> actionable message for the rest.
        """
        from specify_cli.integration_state import (
            default_integration_key,
            try_read_integration_json,
        )
        from specify_cli.integrations import get_integration

        keys: dict[str, str] = {}
        errors: dict[str, str] = {}
        for member in participants:
            if not member.path.is_dir():
                errors[member.id] = f"member path not found: {member.path}"
                continue
            if not (member.path / ".specify").is_dir():
                errors[member.id] = (
                    f"{member.path} is not a Spec Kit project (no .specify/); "
                    "run 'specify init' in the member repository first"
                )
                continue

            key = member.integration
            if not key:
                state, _error = try_read_integration_json(member.path)
                key = default_integration_key(state) if state else None
            if not key:
                errors[member.id] = (
                    "no integration configured; run 'specify init' in the "
                    "member repository or set 'integration' in workspace.yml"
                )
                continue

            impl = get_integration(key)
            if impl is None:
                errors[member.id] = f"unknown integration {key!r}"
                continue
            if impl.build_exec_args("__speckit_multi_repo_probe__") is None:
                errors[member.id] = (
                    f"integration {key!r} does not support non-interactive "
                    "CLI dispatch"
                )
                continue
            keys[member.id] = key
        return keys, errors

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

        # Sequential waves stream agent output live; parallel waves capture
        # (interleaved streams would be unreadable) under a timeout.
        stream = settings["max_concurrency"] <= 1
        try:
            dispatch = impl.dispatch_command(
                settings["command"],
                args=member.tasks_file,
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
