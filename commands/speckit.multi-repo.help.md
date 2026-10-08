---
description: Explain the Spec Kit workflow and the multi-repo workspace flow, and recommend the next command for this repository's role and feature state
scripts:
  sh: scripts/bash/check-prerequisites.sh --json
  ps: scripts/powershell/check-prerequisites.ps1 -Json
  py: scripts/python/check_prerequisites.py --json
---

## User Input

```text
$ARGUMENTS
```

Optional: the user's question, in any language (for example "tenho uma ideia,
por onde começo?", "how does resume work?", "what do I run next?"). When
empty, produce the orientation report described in Step 4 for the current
repository and feature state.

## Purpose

This is the multi-repo extension's help command. It orients the user in the
Spec Kit workflow and in the multi-repo workspace flow, answers "how does
this work" questions, and recommends the concrete next command. It is
**read-only**: it explains and recommends — it never dispatches workflows,
never edits feature files, never modifies the workspace manifest. It works
in the central specs repository and in every member repository.

## Step 1 — Identify this repository's role

Determine the role before answering; the answer depends on it:

1. `.specify/workspace.yml` exists → this is the **central specs
   repository** of a declared workspace. Read it: the member ids, `path`s,
   `depends_on` graph, per-member `constitution` files, and `integration`s.
2. Otherwise, if this repository's `AGENTS.md` contains the
   `<!-- SPECKIT-MULTI-REPO:GOVERNANCE -->` marker → this is a **member
   repository**. Its feature and tasks live in a central specs repository
   elsewhere on disk.
3. Otherwise → a **Spec Kit project with the extension installed but no
   workspace role** — typically a central repository whose manifest was not
   scaffolded yet.

Also note whether `SPECIFY_FEATURE_DIRECTORY` is set (a dispatched, headless
context) and read `.specify/extensions.yml` to list which extensions are
actually installed.

## Step 2 — Determine the feature state

Run `{SCRIPT}` from the repo root and parse FEATURE_DIR. The script honors
`SPECIFY_FEATURE_DIRECTORY`; interactively it may also resolve this repo's
`.specify/feature.json`. If it fails (no feature for the current branch),
treat that as "no active feature" — not an error. When FEATURE_DIR exists,
check which of these exist and report the state they imply:

- `spec.md` / `plan.md` / `tasks.md` → how far the standard SDD cycle went
- `tasks/<member>.md` files → whether `/speckit.multi-repo.tasks` already
  split the tasks
- `governance/` → bundles from a previous dispatch
- the latest run under `.specify/workflows/runs/` → last dispatch outcome
  (completed / failed / awaiting resume)

## Step 3 — Answer from this map

Draw on these facts; do not invent commands or flags beyond them.

**The Spec Kit flow around this extension.** A feature starts with the
standard cycle, run **in the central repository** when the work spans
repositories: constitution once per project; then per feature
`specify` → `plan` → `tasks` (each reviewed before the next; `clarify`,
`checklist`, `analyze` are optional quality gates), then `implement` →
`converge` until convergence reports Converged. An idea that is not yet a
feature may belong to the `assess` extension (evidence first, ending in go /
needs-clarification / kill); a defect belongs to the `bug` extension
(assess → fix → test). Multi-repo dispatch only enters after `tasks`
exist.

**Who runs what, where.**

| Where | Invocation | What it does |
|---|---|---|
| Central, agent chat | `specify` cycle (`speckit.specify`, `speckit.plan`, `speckit.tasks`) | produces the feature's spec, plan, tasks |
| Central, agent chat | `speckit.multi-repo.tasks` | splits `tasks.md` into `tasks/<member>.md` slices, root becomes an orchestration page |
| Central, agent chat | `speckit.multi-repo.workspace` | scaffolds or audits `.specify/workspace.yml` |
| Central, terminal | `specify workflow run multi-repo -i feature=<dir>` | dispatches implementation to members in waves, interactive gates |
| Central, terminal | `specify workflow run multi-repo-check [--json]` | read-only doctor: member readiness + waves, dispatches nothing |
| Central, terminal | `specify workflow resume <run-id>` | re-runs a failed run; completed members are skipped |
| Member, agent chat | `speckit.multi-repo.implement` | implements this member's task slice (usually dispatched headlessly by the orchestrator, not typed) |

Command invocation syntax varies by integration (skills-mode agents register
`speckit-multi-repo-tasks`, others `speckit.multi-repo.tasks`) — quote the
form present in this repository's agent command directory.

**The workspace model.** All feature state lives in the central repository
(spec, plan, orchestration `tasks.md`, per-member task files, governance
bundles, workflow runs); members produce only implementation code, so a
member's PR is a pure implementation diff. Dispatch order is topological
over `depends_on` (human knowledge — never invented), computed per feature
over participating members only. Governance is three constitution layers —
workspace (`central/.specify/memory/constitution.md`), per-member-type file
declared in the manifest, member-local — assembled at dispatch into
`<feature>/governance/<member>.md` and handed to the member's agent;
conflicts resolve to the most restrictive rule.

**Troubleshooting map.**

- A member failed → fix the code in that member repository, then
  `specify workflow resume <run-id>` in the central; members whose task
  files are fully checked off are not dispatched again.
- A member is not cloned locally → re-run with `-i missing=skip`
  (configuration mistakes still abort; `skip` covers local absence only).
- Unclear whether the workspace is healthy → `specify workflow run
  multi-repo-check --json` and inspect the `members` report.
- Tasks were not split → run `speckit.multi-repo.tasks` in the central (the
  `after_tasks` hook offers it automatically after `speckit.tasks`).
- Wrong dispatch order → fix `depends_on` in `.specify/workspace.yml`; the
  graph is never inferred.

## Step 4 — Answer shape

- Answer the user's actual question first, in their language; add only the
  orientation that helps next. When `$ARGUMENTS` was empty, produce a short
  orientation report instead: this repository's role, the feature state from
  Step 2, and one recommended next action.
- Always end with the concrete next command and where it runs (which
  repository, agent chat vs terminal).
- Recommend only what is installed in this repository; when a
  recommendation depends on a missing extension or a missing manifest, say
  what to install or scaffold first (`specify extension add multi-repo`,
  `speckit.multi-repo.workspace`).
- For depth beyond this command, read the extension's own `README.md` and
  `docs/tutorial.md` under `.specify/extensions/multi-repo/` when present,
  instead of guessing.
