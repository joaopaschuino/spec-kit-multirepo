# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.7.0] - 2026-11-11

### Added

- **Domain topology in the workspace manifest**: members accept optional
  `domain` (business domain) and `communicates_with` (member ids the member
  talks to at runtime). `communicates_with` must reference known members
  other than the member itself, but may contain cycles — communication is
  often bidirectional. `depends_on` remains the only execution graph
  (acyclic; waves and `ready: false` holds are unchanged)
- Dry runs (`multi-repo-check`) report each member's `domain` and
  `communicates_with`, and a `topology` output: a Mermaid flowchart with one
  subgraph per domain, dependency edges, and communication edges
- `/speckit.multi-repo.domain` agent command (central repo): maps the domain
  topology from evidence in member repositories — IaC, interface specs,
  config, HTTP/messaging/storage clients, feature `contracts/` — as edges
  with channel, direction, confidence, and `file:line`. `auto` sweeps every
  member and proposes the whole map; `guided [<member>]` walks the flow
  outward from one piece in a conversation where the user adds the domain
  context. Writes `domain`/`communicates_with` after confirmation (never
  `depends_on`) and `.specify/memory/domain.md` (diagram, communication
  table, external systems, domain notes). `/speckit.multi-repo.tasks` and
  `/speckit.multi-repo.workspace` point to it

## [0.6.0] - 2026-11-10

### Added

- **Objective verification (router/worker)**: member task files end with a
  `## Verification` section — POSIX commands for lint, type-check, test, and
  e2e with their expected exit codes (`- test: `npm test` → exit 0`).
  `/speckit.multi-repo.tasks` generates it from each member's real tooling
  and preserves it on re-runs like checkbox state. After each member's
  dispatch the step runs every command in the member repository (`sh`,
  `cwd` = member repo, feature axis exported), logs the output to
  `<feature>/verification/<member>.log`, and records a per-member
  `verification` result (`verified` | `failed` | `skipped`) in the step
  output; a member whose checks fail is `failed`. The run summary is written
  to `<feature>/verification/report.md`
- `verify_retries: N` step option (default `0`; also a `multi-repo` workflow
  input): a failed verification re-dispatches the member's agent up to N
  more times with a feedback file (failing commands, exit codes, output)
  passed as a third command argument
- **Contract-check members**: a task file with a Verification section and no
  task checkboxes is verification-only — no agent is dispatched, its
  commands run after its `depends_on` producers, and its path only has to
  exist (a plain consumer or contracts repository). README example and a
  `tasks/contract-check.md` template in `/speckit.multi-repo.tasks` for
  features with contracts between members (schema diff, consumer-driven
  contract tests against the feature's `contracts/`)
- The step keeps the Status column of the orchestration page
  (`<feature>/tasks.md`) current: `pending → dispatched → verified |
  completed | failed`
- `ready: false` in a member task file's front matter holds that member —
  and its dependents — back (`status: not-ready`, output `not_ready`), so a
  feature can be dispatched in subsets
- The `multi-repo` review gate shows the verification summary and report

### Changed

- `dispatch_timeout` now bounds sequential (streamed) dispatches too, via a
  watchdog — previously only parallel waves were time-bounded. A timed-out
  member is `failed` with `timed_out: true`. The agent stays in the
  orchestrator's process group (terminal behavior unchanged); on timeout its
  process tree is killed, descendants before the agent, so no build/test
  process it spawned keeps writing to the member repository. Verification
  commands share the same bound and tree kill
- With `skip_completed`, a checked-off member that has Verification commands
  is skipped only when its checks pass; otherwise it is re-dispatched with
  feedback
- `speckit.multi-repo.implement` runs the task file's Verification commands
  during validation, reads the retry feedback file when present, and must
  never weaken the Verification section

## [0.5.0] - 2026-10-07

### Added

- `speckit.multi-repo.help` agent command, installed with the extension in
  the central and every member repository: answers orientation questions in
  the agent chat — where to start with an idea, which command runs next for
  the current repository and feature state, how waves, resume, and
  governance work. It detects the repository's role (central / member /
  pre-scaffold project) and the active feature state before answering, and
  is read-only: it explains and recommends, never dispatches workflows or
  edits files

## [0.4.0] - 2026-10-07

### Added

- **Governance bundles**: before dispatching a member, the orchestrator
  assembles every constitution layer that exists (workspace, member/type,
  member-local) into one Markdown file per member under
  `<feature>/governance/<member>.md`, and passes its absolute path as the
  dispatched command's second argument. The member's agent reads one
  authoritative file instead of resolving layer paths itself; the bundle
  doubles as a per-dispatch audit artifact of exactly which rules each
  member received
- Fail-fast readability validation: a constitution file that exists but
  cannot be read (permissions, encoding) fails the run before any dispatch
- `install.sh member` now maintains a "Multi-repo workspace governance"
  section in the member's `AGENTS.md` (idempotent, dedicated
  `<!-- SPECKIT-MULTI-REPO:GOVERNANCE -->` markers): the persistent channel
  that keeps free-form edits — which bypass the implement command — subject
  to the workspace constitution layers
- **Upgrading in place**: `install.sh` now replaces already-installed
  components (`--force` on the extension and the custom step; workflows
  overwrite by default), making a re-run the supported upgrade path —
  documented in the README's new "Upgrading an existing workspace" section
- `SPECIFY_BIN` environment variable on `install.sh`: point at a specific
  `specify` binary when `PATH` carries one older than 1.1.0

### Changed

- Dispatch protocol: the second command argument is now the governance
  bundle path (previously the member/type constitution path, only when
  configured). The bundle is dispatched uniformly, even when no layer
  exists, so the member command never has to guess which layers apply.
  Upgrade the extension and the custom workflow step together
- `speckit.multi-repo.implement`: governance step reads the bundle when
  dispatched; the manual three-layer path resolution remains as the
  standalone interactive fallback

### Upgrade

- Re-run the same `install.sh` command you used originally on the central
  repository and on each member — it now upgrades in place (replaces the
  extension and the custom step, re-adds the workflows, refreshes the
  members' `AGENTS.md` governance section). Verify with
  `specify workflow run multi-repo-check`

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
