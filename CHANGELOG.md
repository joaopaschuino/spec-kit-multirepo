# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.0] - 2026-10-04

### Added

- `dry_run` setting on the orchestrator step and the new `multi-repo-check`
  workflow: a read-only workspace report — every declared member classified
  (`ready` / `not-cloned` / `not-initialized` / `no-integration` /
  `unknown-integration` / `no-dispatch` / `constitution-missing`),
  participation and dispatch waves computed, nothing dispatched. A
  feature-less dry run reports the whole declared workspace
- `missing` setting (`error` default | `skip`): with `skip`, participants
  that are not cloned or not initialized locally are recorded as
  `status: missing` (with reason) in the results while the ready members
  dispatch; configuration errors still abort in both modes
- `speckit.multi-repo.workspace` agent command: scaffolds
  `.specify/workspace.yml` from sibling Spec Kit projects (never inventing
  `depends_on`) or audits an existing manifest (readiness table, drift
  detection, confirmed additions only)
- `dry_run` and `missing` exposed as inputs on the `multi-repo` workflow

### Upgrade

- Reinstall the extension and the custom workflow step; re-add both
  workflows (`multi-repo` gained inputs, `multi-repo-check` is new)
- **Spec Kit ≥ 1.1.0 is now required**: installing the custom step needs
  `specify workflow step add --dev/--from`, introduced in Spec Kit 1.1.0.
  On 1.0.x, copy the step package by hand:
  `cp -R step/multi-repo-implement <central>/.specify/workflows/steps/`

## [0.2.0] - 2026-10-04

### Added

- Optional per-member `constitution` field in the workspace manifest: a
  shared governance file stored in the central repository (for example
  `constitutions/lambda.md`). Every member pointing at the same file shares
  it, so repositories of the same type read one constitution instead of
  keeping copies
- Three-layer governance in member implementation: workspace constitution
  (central `.specify/memory/constitution.md`) → member/type constitution
  (the shared file above) → member-local constitution, with the most
  restrictive rule winning; the member's completion report names the layers
  that applied
- Fail-fast validation: a dispatch run fails before any dispatch when a
  member's configured constitution file does not exist in the central
  repository
- The member's constitution path travels in the dispatched command
  arguments (safe under concurrent waves) and is recorded in the per-member
  step results

### Upgrade

- Reinstall the extension and the custom workflow step (both changed); the
  workflow definition is unchanged

## [0.1.0] - 2026-10-04

### Added

- Workspace manifest (`.specify/workspace.yml`): central specs repository
  declares member repositories with `path`, `integration`, `tasks_file`, and
  `depends_on` (validated: unique ids, known dependencies, acyclic graph)
- `multi-repo-implement` custom workflow step: dependency-wave (Kahn)
  dispatch of `speckit.multi-repo.implement` to each member repo headlessly,
  with `SPECIFY_FEATURE_DIRECTORY` exported, `skip_completed`,
  `stop_on_failure`, per-wave `max_concurrency`, and per-member results in
  the step output
- `multi-repo` workflow: confirm gate → orchestrated dispatch → review gate,
  with CI-friendly verdict inputs
- `speckit.multi-repo.tasks` command (central): splits the feature's
  `tasks.md` into per-member `tasks/<id>.md` files and an orchestration page
- `speckit.multi-repo.implement` command (member): headless-friendly member
  implementation slice, reading spec/plan/tasks from the central feature
  directory
- `after_tasks` optional hook offering the task split
- `install.sh` with local (`--dev`) and GitHub-release installation modes
- Test suite: manifest parser/validator, step execution, and an end-to-end
  install/run/resume flow against a fake agent executable
