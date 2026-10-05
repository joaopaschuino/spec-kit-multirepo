---
description: Implement this repository's slice of a centrally-specified feature by executing the tasks in this member's task file
scripts:
  sh: scripts/bash/check-prerequisites.sh --json --require-tasks --include-tasks
  ps: scripts/powershell/check-prerequisites.ps1 -Json -RequireTasks -IncludeTasks
  py: scripts/python/check_prerequisites.py --json --require-tasks --include-tasks
---

## User Input

```text
$ARGUMENTS
```

You **MUST** consider the user input before proceeding (if not empty). When this command is dispatched by the multi-repo orchestrator, `$ARGUMENTS` contains the member's tasks file path **relative to the feature directory** (for example `tasks/lambda.md`) and, when the member declares a workspace constitution, that constitution file's **absolute path** as a second token.

## Purpose

This repository is a **member** of a multi-repo workspace. The active feature is specified in a **central specs repository**: `FEATURE_DIR` (resolved below) points into that central repository, not this one. You implement code **in this repository only**, following the member's task file from the central feature directory.

## Pre-Execution Checks

**Check for extension hooks (before implementation)**:
- Check if `.specify/extensions.yml` exists in the project root.
- If it exists, read it and look for entries under the `hooks.before_implement` key
- If the YAML cannot be parsed or is invalid, do not skip silently: tell the user that `.specify/extensions.yml` could not be read (include the parser error) and that no hooks were checked, including any mandatory (`optional: false`) hooks registered there, then continue normally
- Filter out hooks where `enabled` is explicitly `false`. Treat hooks without an `enabled` field as enabled by default.
- For each remaining hook, do **not** attempt to interpret or evaluate hook `condition` expressions:
  - If the hook has no `condition` field, or it is null/empty, treat the hook as executable
  - If the hook defines a non-empty `condition`, skip the hook and leave condition evaluation to the HookExecutor implementation
- For each executable hook, output the following based on its `optional` flag:
  - **Optional hook** (`optional: true`):
    ```
    ## Extension Hooks

    **Optional Pre-Hook**: {extension}
    Command: `/{command}`
    Description: {description}

    Prompt: {prompt}
    To execute: `/{command}`
    ```
  - **Mandatory hook** (`optional: false`):
    ```
    ## Extension Hooks

    **Automatic Pre-Hook**: {extension}
    Executing: `/{command}`
    EXECUTE_COMMAND: {command}

    Wait for the result of the hook command before proceeding to the Outline.
    ```
    After emitting the block above you MUST actually invoke the hook and wait for it to finish before continuing. Run it the same way you would run the command yourself in this agent/session (the invocation may differ from the literal `{command}` id shown above, e.g. a skills-mode agent runs it as `/skill:speckit-...` or `$speckit-...`). Emitting the block alone does not run the hook.
- If no hooks are registered or `.specify/extensions.yml` does not exist, skip silently

## Outline

1. Run `{SCRIPT}` from repo root and parse FEATURE_DIR and AVAILABLE_DOCS list. All paths must be absolute. The scripts honor `SPECIFY_FEATURE_DIRECTORY`, which the orchestrator sets to the central feature directory; when run interactively, FEATURE_DIR may also come from this repo's `.specify/feature.json`.

2. **Resolve TASKS_FILE**, in this priority order:
   - `$ARGUMENTS` if non-empty (a path relative to FEATURE_DIR, e.g. `tasks/lambda.md`)
   - the `SPECKIT_MULTI_REPO_TASKS_FILE` environment variable
   - `implement.tasks_file` in `.specify/extensions/multi-repo/multi-repo-config.yml`, if set
   - otherwise, ask the user which member task file to implement (interactive only)

   Then verify `FEATURE_DIR/TASKS_FILE` exists. If it does not, **STOP** with:
   `Member task file not found: FEATURE_DIR/TASKS_FILE. Run /speckit.multi-repo.tasks in the central specs repository to split tasks across members.`

   Do **not** fall back to the feature's root `tasks.md`: in a multi-repo workspace that file is an orchestration page, not this member's work list.

3. Load and analyze the implementation context:
   - **REQUIRED**: Read `FEATURE_DIR/spec.md` for the feature requirements
   - **REQUIRED**: Read `FEATURE_DIR/TASKS_FILE` for this member's task list (the ONLY task list you execute)
   - **IF EXISTS**: Read `FEATURE_DIR/plan.md` for tech stack, architecture, and file structure
   - **IF EXISTS**: Read `FEATURE_DIR/data-model.md`, `FEATURE_DIR/contracts/`, `FEATURE_DIR/research.md`, `FEATURE_DIR/quickstart.md`

4. **Governance — read every layer that exists; when layers conflict, the most restrictive rule wins.** Each layer narrows the one above it, never loosens it:
   1. *Workspace*: `<central-root>/.specify/memory/constitution.md` — the central root is two levels above FEATURE_DIR
   2. *Member/type*: the second token of `$ARGUMENTS`, when present — the absolute path to this member's shared constitution file in the central repository (for example `constitutions/lambda.md`, shared by every member of the same type). When absent, look up this repository's entry in `<central-root>/.specify/workspace.yml` (the member whose `path` resolves to the current working directory) and read its `constitution` file relative to the central root, if set
   3. *Local*: this repository's `.specify/memory/constitution.md`, if it exists

   Type constitutions hold only rules true for the whole type (runtime, naming, mandatory checks); repo-specific exceptions belong in the local layer.

5. **Checklist handling** (if `FEATURE_DIR/checklists/` exists):
   - Build the same status table the standard implement command uses (per checklist: total / checked / unchecked)
   - **If you are running headless** (dispatched non-interactively, no user to answer): display the table, note any unchecked items in your final report, and **continue** — do not block
   - **If you are running interactively**: follow the standard behavior — STOP and ask before proceeding when any checklist has unchecked items
   - Never modify checklist files or markers

6. **Project setup verification**: create or verify ignore files for this repository's detected stack (a git repo needs `.gitignore`; Dockerfile → `.dockerignore`; and so on for eslint/prettier/terraform as applicable). If an ignore file already exists, append only missing essential patterns. Use the common patterns for the languages and tooling found in this repo.

7. Parse `FEATURE_DIR/TASKS_FILE` structure and extract:
   - Task phases and their ordering (Setup, Tests, Core, Integration, Polish or as written)
   - Task dependencies: sequential vs parallel markers `[P]`
   - Task details: ID, description, file paths

8. Execute implementation following the member task file:
   - Phase-by-phase execution: complete each phase before moving to the next
   - Respect dependencies: sequential tasks in order; `[P]` tasks may be treated as parallelizable
   - Follow TDD where the task list pairs test and implementation tasks
   - Write code, configuration, and documentation **in this repository** (the current working directory)
   - When a task depends on a cross-repo contract (event schema, bucket policy, API shape), treat `FEATURE_DIR/contracts/` as the authority; do not invent divergent copies
   - Where a governance rule constrains a task (layer 1–4 above), the rule wins over the task text

9. Progress tracking and error handling:
   - Report progress after each completed task
   - Halt execution if any non-parallel task fails; for `[P]` tasks continue with the successful ones and report failures
   - **IMPORTANT**: For completed tasks, mark the task as `[X]` in `FEATURE_DIR/TASKS_FILE` (the file lives in the central specs repository — write your checkbox updates there, not to a local copy)

10. Validation:
    - Run this repository's tests and linters; fix what the implementation broke
    - Verify the implementation matches `FEATURE_DIR/spec.md` for this member's scope

11. Completion report:
    - Tasks completed / total from `FEATURE_DIR/TASKS_FILE`
    - Files created or modified in this repository
    - Test and lint results
    - Governance layers applied (which of the three existed), and any rule that overrode a task
    - Any cross-repo contract consumed, and any checklist items left unchecked

## Mandatory Post-Execution Hooks

**You MUST complete this section before reporting completion to the user.**

Check if `.specify/extensions.yml` exists in the project root.
- If it does not exist, or no hooks are registered under `hooks.after_implement`, skip to the Completion Report.
- If it exists, read it and look for entries under the `hooks.after_implement` key.
- If the YAML cannot be parsed or is invalid, do not skip silently: tell the user that `.specify/extensions.yml` could not be read (include the parser error) and that no hooks were checked, including any mandatory (`optional: false`) hooks registered there, then continue to the Completion Report.
- Filter out hooks where `enabled` is explicitly `false`. Treat hooks without an `enabled` field as enabled by default.
- For each remaining hook, do **not** attempt to interpret or evaluate hook `condition` expressions:
  - If the hook has no `condition` field, or it is null/empty, treat the hook as executable
  - If the hook defines a non-empty `condition`, skip the hook and leave condition evaluation to the HookExecutor implementation
- For each executable hook, output the following based on its `optional` flag:
  - **Mandatory hook** (`optional: false`) — **You MUST emit `EXECUTE_COMMAND:` for each mandatory hook**:
    ```
    ## Extension Hooks

    **Automatic Hook**: {extension}
    Executing: `/{command}`
    EXECUTE_COMMAND: {command}
    ```
    After emitting the block above you MUST actually invoke the hook and wait for it to finish before continuing. Run it the same way you would run the command yourself in this agent/session (the invocation may differ from the literal `{command}` id shown above, e.g. a skills-mode agent runs it as `/skill:speckit-...` or `$speckit-...`). Emitting the block alone does not run the hook.
  - **Optional hook** (`optional: true`):
    ```
    ## Extension Hooks

    **Optional Hook**: {extension}
    Command: `/{command}`
    Description: {description}

    Prompt: {prompt}
    To execute: `/{command}`
    ```
- Then proceed to the Completion Report.
