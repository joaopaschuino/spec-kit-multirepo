---
description: Split the active feature's tasks.md into per-member task files based on the workspace manifest
scripts:
  sh: scripts/bash/check-prerequisites.sh --json --require-tasks --include-tasks
  ps: scripts/powershell/check-prerequisites.ps1 -Json -RequireTasks -IncludeTasks
  py: scripts/python/check_prerequisites.py --json --require-tasks --include-tasks
---

## User Input

```text
$ARGUMENTS
```

You **MUST** consider the user input before proceeding (if not empty). The user may name the members that participate in this feature; otherwise participation is inferred from the task content.

## Purpose

This repository is the **central specs repository** of a multi-repo workspace. The feature's implementation is spread across member repositories declared in `.specify/workspace.yml`. This command splits the feature's root `tasks.md` into one task file per participating member and rewrites the root `tasks.md` as an orchestration page.

## Outline

1. Run `{SCRIPT}` from repo root and parse FEATURE_DIR and AVAILABLE_DOCS list. All paths must be absolute.

2. **Load the workspace manifest**: read `.specify/workspace.yml` from the repo root. If it does not exist or cannot be parsed, **STOP** with:
   `No workspace manifest found at .specify/workspace.yml. This command must run in the central specs repository of a multi-repo workspace (see the multi-repo extension README).`
   Note each member's `id`, `path`, `tasks_file` (default `tasks/<id>.md`), and `depends_on`.

3. Read `FEATURE_DIR/tasks.md` (the task list produced by the standard tasks command) and, for context, `FEATURE_DIR/spec.md` and `FEATURE_DIR/plan.md` if they exist.

4. **Classify each task to a member** (or to "central"):
   - A task belongs to a member when its target files, services, or explicit mentions match that member (e.g. paths under the member's repository, its AWS resource types — Lambda, Glue, SQS, Step Functions — or a `repo: <id>` tag in the task text)
   - A task spanning several members is itself a **contract task**: it belongs in the feature's `contracts/` area (or the central slice), not duplicated into every member
   - A task about specs, docs, or orchestration belongs to "central"
   - When running interactively and a task's owner is ambiguous, ask the user; when running headless, assign it to "central" and flag it in the report

5. **Write each member's task file** at `FEATURE_DIR/<member.tasks_file>`:
   - Start with a short header: feature name, member id, the member's `depends_on` list mirrored from the workspace manifest, and — when the member declares `constitution` in the manifest — the constitution file path (all informational)
   - Copy the member's tasks **preserving task IDs, phases, `[P]` parallel markers, and checkbox state** verbatim
   - Include a "Shared context" section pointing to `spec.md`, `plan.md`, and `contracts/` (relative to the feature directory) so the member's implement command can find them

6. **Rewrite `FEATURE_DIR/tasks.md` as the orchestration page**:
   - Keep the feature title and a two-line summary of what the feature delivers
   - Replace the task dump with a mirror table, one row per participating member:

     | Member | Task file | Depends on | Status |
     |--------|-----------|------------|--------|
     | s3 | tasks/s3.md | — | pending |
     | lambda | tasks/lambda.md | s3 | pending |

   - Generate the **Depends on** column from `.specify/workspace.yml` (`depends_on`), never guess it from the tasks — the manifest is the source of truth for dispatch order; this table is a human-readable mirror
   - Keep central/contract tasks listed under a separate "Central" section
   - Status column starts as `pending` for every member and is updated later (by humans or agents) as work completes

7. **Report**: members detected vs participating, task counts per member, tasks left in the central slice, and any ambiguous classifications you resolved.

## Notes

- Re-running this command is safe: it regenerates member task files and the orchestration page, but **must preserve existing checkbox state** in member task files (append missing tasks, do not reset completed ones)
- If the feature genuinely touches a single repository, say so and suggest running the standard implement command instead — no split is needed
