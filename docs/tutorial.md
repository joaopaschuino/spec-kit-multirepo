# Tutorial: your first multi-repo feature

This walkthrough builds a minimal workspace from scratch — one central specs
repository plus two member repositories — and takes a feature all the way
from specification to dispatched implementation, including a failure and
resume. It mirrors what the automated end-to-end test does, but with real
agent CLIs.

Time: ~30 minutes. Prerequisites:

- `specify` CLI ≥ 0.9.0 installed
- A coding agent CLI installed **and authenticated** (this tutorial uses
  `claude`; `copilot`, `gemini`, `codex`, … work the same way)
- Git

## 0. Create the three repositories

```sh
mkdir multirepo-demo && cd multirepo-demo

# central specs repository — Spec Kit drives the SDD cycle here
git init central && cd central
specify init --here --integration claude --script sh --ignore-agent-tools
git add -A && git commit -m "init central specs repo"
cd ..

# member repositories — code lands here
for repo in repo-s3 repo-lambda; do
  git init "$repo" && cd "$repo"
  specify init --here --integration claude --script sh --ignore-agent-tools
  git add -A && git commit -m "init $repo"
  cd ..
done
```

Each member keeps the full standard Spec Kit command set installed; the
multi-repo extension adds one more command to them (`speckit.multi-repo.implement`).

## 1. Install the extension

From a release (see the [README](../README.md#install) for URLs), or from a
clone of this repository:

```sh
git clone https://github.com/joaopaschuino/spec-kit-multirepo
cd spec-kit-multirepo

# central: agent commands + custom step + workflow
./install.sh central ../central

# members: agent commands only
./install.sh member ../repo-s3
./install.sh member ../repo-lambda
```

Commit the results in each repository (`.specify/` and `.claude/` changes).

## 2. Declare the workspace

Create `.specify/workspace.yml` in the **central** repository:

```yaml
schema_version: "1.0"
workspace:
  id: demo-platform
  name: "Demo Platform"
members:
  - id: s3
    path: ../repo-s3
  - id: lambda
    path: ../repo-lambda
    constitution: constitutions/lambda.md  # shared governance, live in central
    depends_on: [s3]     # s3 (the producer) dispatches first
```

`path` is relative to the central root — sibling checkouts in this tutorial.
A git-submodule layout works too (point `path` at the member checkout).

Then create the shared constitution for the lambda type in the central repo
(keep the workspace-level one at `central/.specify/memory/constitution.md`,
edited with `/speckit.constitution` as usual):

```sh
mkdir -p central/constitutions
cat > central/constitutions/lambda.md <<'EOF'
# Lambda type constitution
- Runtime and naming conventions every Lambda member must follow
- Every handler validates its event against the feature contracts/ first
EOF
```

At dispatch time each member's agent reads, in order, the workspace
constitution → this type constitution (its path is validated and passed by
the orchestrator) → the member's own `.specify/memory/constitution.md`, with
the most restrictive rule winning. A configured but missing constitution
file fails the run before any dispatch.

## 3. Specify, plan, and generate tasks (central)

Open your agent in `central/` and run the standard Spec Kit cycle:

```
/speckit.specify Add an S3 bucket that receives uploads and a Lambda that
consums new objects from it and logs their size

/speckit.plan ...

/speckit.tasks ...
```

Approve the gates. You now have `central/specs/001-<slug>/` with `spec.md`,
`plan.md`, and a single `tasks.md`.

## 4. Split the tasks across members (central)

Still in the agent, run:

```
/speckit.multi-repo.tasks
```

(The `after_tasks` hook offers this automatically right after
`/speckit.tasks`.)

The command creates `specs/001-<slug>/tasks/s3.md` and
`specs/001-<slug>/tasks/lambda.md` (one per participating member) and
rewrites the root `tasks.md` as an orchestration page with a mirror table.
Review the split — move tasks between members if the classification missed
one. Keep contract work (the object prefix, event schema, bucket policy)
in the central slice or `contracts/`.

## 5. Dispatch the implementation

Back in your shell, in `central/`:

```sh
specify workflow run multi-repo
```

What happens:

1. **confirm gate** — approve (interactive prompt).
2. **implement step** — reads `workspace.yml`, finds members with a task
   file, computes waves (`[s3]` then `[lambda]`), and dispatches
   `/speckit-multi-repo-implement tasks/s3.md` headlessly with
   `cwd=repo-s3`, then the same for `lambda` after s3 succeeds. Each
   member's agent reads the central spec/plan and implements **its own
   repository**, marking checkboxes in its central task file.
3. **review gate** — approve to finish.

Non-interactive (CI):

```sh
specify workflow run multi-repo \
  -i feature=specs/001-<slug> -i confirm=approve -i review=approve --json
```

> **Agent permissions**: headless agents may need permission flags — e.g.
> `SPECKIT_INTEGRATION_CLAUDE_EXTRA_ARGS="--dangerously-skip-permissions"`.
> Other agents have equivalents (`--yolo`, `--always-approve`, …); see
> `docs/reference/integrations.md` in the Spec Kit repository.

## 6. Inspect the results

```sh
specify workflow status <run-id>     # per-step status
cat .specify/workflows/runs/<run-id>/state.json | jq '.step_results.implement.output'
```

The step output records `waves`, per-member `results` (status, exit code,
task file), and `failed`. In each member, `git status` shows the changes;
in the central feature directory, the task checkboxes are marked.

## 7. Break it and resume it

To see failure handling: revert `repo-lambda` to a broken state (e.g. `git
reset --hard` to drop its changes but keep its task file unchecked), or make
its dispatch fail, then re-run:

```sh
specify workflow run multi-repo          # s3 completes, lambda fails → run fails
# fix whatever broke lambda...
specify workflow resume <run-id>         # re-dispatches only what's left
```

Members whose task files are fully checked are **skipped** on resume
(`skip_completed: true` by default).

## 8. Work interactively inside a member

The dispatched command also works standalone. Open your agent inside
`repo-lambda/` and run:

```
/speckit-multi-repo-implement
```

It asks which task file to implement (or takes it from the extension config
/ `SPECIFY_MULTI_REPO_TASKS_FILE`), reads the central feature directory, and
implements in `repo-lambda` — same flow, you in the loop instead of the
orchestrator.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `No workspace manifest found at .specify/workspace.yml` | Command or workflow run outside the central repo; create the manifest there |
| `Member validation failed ... not a Spec Kit project` | Member missing `.specify/` — run `specify init` in it |
| `Member validation failed ... no integration configured` | Member has no `integration.json` — init with `--integration`, or set `integration:` in `workspace.yml` |
| `Member validation failed ... does not support non-interactive CLI dispatch` | That agent has no headless mode; pick another integration for the member |
| `No workspace members participate in this feature` | No `tasks/<member>.md` under the feature directory — run `/speckit.multi-repo.tasks` |
| `Feature directory not found` | Bad `feature` input — pass `-i feature=specs/001-<slug>` |
| Agent starts then fails on permissions | Set the integration's extra-args env var (see step 5 note) |
| Dispatch order looks wrong | Waves come from `depends_on` in `workspace.yml` — the tasks.md table is only a mirror |

## Where to go next

- [README](../README.md) — full reference for the manifest, step settings,
  and install modes
- The automated version of this tutorial: `tests/test_e2e_workflow.py`
- Once a real feature has flown through the workspace, collect your metrics
  (integration rework, spec→PR time) — they are the evidence for proposing
  multi-repo support upstream in Spec Kit
