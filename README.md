# multi-repo — Spec Kit workspace orchestration

Orchestrate spec-driven development across a **multi-repo workspace**: one
**central specs repository** owns the feature specs, and implementation is
dispatched headlessly to each **member repository** (Lambda, Glue, SQS, Step
Functions, …) in dependency order.

This is a Spec Kit extension package (Route B): it uses only stable extension
surfaces — an extension (agent commands + hooks), a custom workflow step, and
a workflow definition — without modifying the Spec Kit core.

## How it works

```
central specs repo                    member repos
┌───────────────────────────┐        ┌──────────────────┐
│ specs/001-feature/        │        │ repo-s3          │
│   spec.md  plan.md        │  wave  │  .specify/       │
│   tasks.md (orchestration)│ ─────► │  agent runs the  │
│   tasks/s3.md             │        │  implement slice │
│   tasks/lambda.md         │        ├──────────────────┤
│ .specify/workspace.yml    │  wave  │ repo-lambda      │
│   members + depends_on    │ ─────► │  (depends on s3) │
└───────────────────────────┘        └──────────────────┘
```

1. Spec, plan, and tasks are produced in the central repo with the standard
   Spec Kit commands.
2. `/speckit.multi-repo.tasks` splits `tasks.md` into `tasks/<member>.md`
   files and rewrites the root `tasks.md` as an orchestration page.
3. `specify workflow run multi-repo` dispatches `speckit.multi-repo.implement`
   to each member with a participating task file, in topological waves
   computed from `workspace.yml`. Each member's agent runs with its own repo
   as the working directory and the central feature directory exported via
   `SPECIFY_FEATURE_DIRECTORY`.
4. The workflow's gates keep a human in the loop; `workflow resume` picks up
   after a member failure, skipping members whose task files are fully
   checked off.

## Install

Requirements: Spec Kit ≥ 0.9.0, member repos initialized with
`specify init --integration <agent>`.

### From a GitHub release (no clone needed)

In each **member repository**:

```sh
specify extension add multi-repo --from \
  https://github.com/joaopaschuino/spec-kit-multirepo/archive/refs/tags/v0.1.0.zip
```

In the **central specs repository**, the two commands above plus:

```sh
specify workflow step add multi-repo-implement --from \
  https://github.com/joaopaschuino/spec-kit-multirepo/releases/download/v0.1.0/step-multi-repo-implement-v0.1.0.zip
specify workflow add multi-repo --from \
  https://raw.githubusercontent.com/joaopaschuino/spec-kit-multirepo/v0.1.0/workflow/multi-repo/workflow.yml
```

(The step install asks an interactive trust confirmation before downloading —
default-deny; answer `yes`.)

### From a clone

```sh
./install.sh central /path/to/central-repo                # local (--dev) mode
./install.sh member  /path/to/member-repo --release v0.1.0 # release mode
```

`install.sh` runs, respectively:

```sh
specify extension add <clone-dir> --dev
specify workflow step add multi-repo-implement --dev <clone-dir>/step/multi-repo-implement
specify workflow add <clone-dir>/workflow/multi-repo --dev
```

### Repository topologies

`workspace.yml` member `path`s are relative to the central root, so both
layouts work:

- **Sibling checkouts**: `path: ../repo-lambda` (simplest; what the tests use)
- **Git submodule of the specs repo inside each member**: point the member
  entry at the member checkout location relative to the central repo

## Workspace manifest — `.specify/workspace.yml`

```yaml
schema_version: "1.0"
workspace:
  id: my-platform
  name: "My Platform"
members:
  - id: lambda
    path: ../repo-lambda
    integration: claude        # optional; default: member's integration.json
    tasks_file: tasks/lambda.md  # optional; default: tasks/<id>.md
    depends_on: []               # members that must complete first
  - id: s3
    path: ../repo-s3
```

Validation: unique slug ids, `path` must be a Spec Kit project at dispatch
time, `depends_on` must reference known members, and the graph must be
acyclic. Waves are computed per feature over the participating subset only —
a member not in the feature never blocks its dependents.

## The workflow step — `multi-repo-implement`

```yaml
- id: implement
  type: multi-repo-implement
  feature: "specs/001-my-feature"   # default: active feature
  repos: [s3, lambda]               # default: members with a tasks file
  command: speckit.multi-repo.implement
  max_concurrency: 1                # per-wave parallelism (captured output)
  dispatch_timeout: 3600            # seconds, parallel mode only
  skip_completed: true              # skip members whose tasks are all [x]
  persist_feature: false            # keep feature.json in members
  stop_on_failure: true             # halt before the next wave on failure
```

Output (usable in later steps / gates via `{{ steps.implement.output.* }}`):
`feature`, `waves` (list of member-id lists), `results` (per-member
`repo`, `wave`, `status` completed|failed|skipped, `exit_code`,
`tasks_file`), and `failed`.

Dispatch details: each member's agent CLI runs headless (`claude -p …`,
`copilot -p … --yolo`, …) with `cwd` = the member repo. Environment for every
dispatch: `SPECIFY_FEATURE_DIRECTORY=<central feature dir>` and
`SPECIFY_FEATURE_NO_PERSIST=1` (unless `persist_feature: true`). The member's
tasks file is passed as the command argument, not as an environment variable,
so concurrent waves cannot race on it.

Resume semantics: the engine re-executes the step from the top on
`workflow resume`. That is safe because re-dispatching a member whose tasks
are complete is a no-op for the agent, and `skip_completed` avoids even that
dispatch.

## Usage walkthrough

```sh
# central repo — usual SDD cycle for the feature
/speckit.specify <description>     # gate: approve
/speckit.plan <description>        # gate: approve
/speckit.tasks <description>

# split tasks across members (also offered automatically by the after_tasks hook)
/speckit.multi-repo.tasks

# dispatch implementation (interactive gates)
specify workflow run multi-repo -i feature=specs/001-my-feature

# or fully non-interactive (CI): pre-approve the gates via inputs
specify workflow run multi-repo \
  -i feature=specs/001-my-feature -i confirm=approve -i review=approve --json

# a member failed? fix it, then:
specify workflow resume <run-id>
```

Inside a member repo, `/speckit.multi-repo.implement` also works standalone
(interactively) — it resolves the feature via `SPECIFY_FEATURE_DIRECTORY` or
the member's persisted `feature.json`, and the member's task file via
argument, `SPECKIT_MULTI_REPO_TASKS_FILE`, or the extension config.

## Design notes — the path to a native feature (Route C)

The manifest is deliberately shaped as a candidate native contract:

- `workspace.yml` members (`path`, `integration`, `tasks_file`,
  `depends_on`) map one-to-one onto a hypothetical core step field
  (`project:`/`depends_on`) and workspace resolution in the engine
- The step implements "dumb executor, graph in data": participation comes
  from the feature's `tasks/` directory, order from the manifest — nothing is
  hardcoded in scripts
- If accepted upstream, the extension step becomes a built-in step type and
  the extension commands can merge into the core templates without a format
  break for existing workspaces

Until then, this package is the reference implementation to collect usage
evidence (integration rework, spec→PR lead time, breaking-change rate) for
an upstream RFC.

## Limitations (v1)

- No cascading constitutions (global → repo-type → local): copy or submodule
  the workspace-level constitution manually; member prompts read both the
  member's and the central `.specify/memory/constitution.md` when present
- No automatic cross-repo contract synchronization: follow the
  [contract-driven development guide](https://github.com/github/spec-kit/blob/main/docs/guides/contract-driven-development.md)
  and keep contracts under the feature's `contracts/` directory
- No bundle/catalog packaging yet: install via `install.sh` (local paths) or
  adapt the three `--dev` commands to URLs
- Parallel waves capture agent output instead of streaming it (interleaved
  live streams would be unreadable); sequential waves stream live

## Development

The test suite needs the `specify_cli` package importable — run it from a
[Spec Kit checkout](https://github.com/github/spec-kit) with this repository
cloned inside it, or from any virtualenv that has `specify-cli` installed:

```sh
# from a spec-kit checkout with this repo at ./multi-repo (or adjust the path)
uv sync --extra test
.venv/bin/pytest multi-repo/tests

# from this repository, using that venv
/path/to/spec-kit/.venv/bin/pytest tests
```

`tests/test_workspace.py` covers the manifest parser/validator and wave
computation; `tests/test_step.py` covers step execution with a stubbed
integration; `tests/test_e2e_workflow.py` installs the components through the
real CLI and runs/resumes a workflow against a fake agent executable
(`SPECKIT_INTEGRATION_CLAUDE_EXECUTABLE`).

## Releasing

```sh
./make-release.sh            # builds dist/step-multi-repo-implement-vX.Y.Z.zip
git tag vX.Y.Z && git push origin vX.Y.Z
gh release create vX.Y.Z dist/step-multi-repo-implement-vX.Y.Z.zip --title vX.Y.Z
```

The extension installs straight from the tag archive; the custom step ships
as a dedicated release asset because `specify workflow step add --from`
expects `step.yml` at the archive root. Update `extension.yml` version,
`CHANGELOG.md`, and the README install URLs together (semver).

