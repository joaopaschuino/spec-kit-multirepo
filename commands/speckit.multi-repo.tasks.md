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

3. Read `FEATURE_DIR/tasks.md` (the task list produced by the standard tasks command) and, for context, `FEATURE_DIR/spec.md` and `FEATURE_DIR/plan.md` if they exist. When `.specify/memory/domain.md` exists (written by `/speckit.multi-repo.domain`), read it too: the domain map says which members communicate over which channel, which helps assign integration tasks to the right owner and spot contract tasks.

4. **Classify each task to a member** (or to "central"):
   - A task belongs to a member when its target files, services, or explicit mentions match that member (e.g. paths under the member's repository, its AWS resource types — Lambda, Glue, SQS, Step Functions — or a `repo: <id>` tag in the task text)
   - A task spanning several members is itself a **contract task**: it belongs in the feature's `contracts/` area (or the central slice), not duplicated into every member
   - A task about specs, docs, or orchestration belongs to "central"
   - When running interactively and a task's owner is ambiguous, ask the user; when running headless, assign it to "central" and flag it in the report

5. **Write each member's task file** at `FEATURE_DIR/<member.tasks_file>`:
   - Start with a short header: feature name, member id, the member's `depends_on` list mirrored from the workspace manifest, and — when the member declares `constitution` in the manifest — the constitution file path (all informational)
   - Copy the member's tasks **preserving task IDs, phases, `[P]` parallel markers, and checkbox state** verbatim
   - Include a "Shared context" section pointing to `spec.md`, `plan.md`, and `contracts/` (relative to the feature directory) so the member's implement command can find them
   - End with a `## Verification` section: the objective checks the orchestrator runs **in the member repository** after the member's agent finishes. The member only counts as done when every command exits with its expected code — the agent's own report is not enough. Format (parsed mechanically — one bullet per command, `label: `command` → exit N`; the exit code defaults to 0 when omitted):

     ```markdown
     ## Verification

     - lint: `npm run lint` → exit 0
     - type-check: `npx tsc --noEmit` → exit 0
     - test: `npm test` → exit 0
     - e2e: `npm run test:e2e` → exit 0
     ```

     - Cover **lint, type-check, test, and e2e** where the member has them. Derive each command from the member's real tooling (its `package.json` scripts, `Makefile`, `pyproject.toml`, `go.mod`, CI workflow, or `plan.md`) — read the member repository at its manifest `path` when it is available; never invent a script that does not exist. Omit a category the member does not have instead of writing a placeholder
     - Every command must be a single non-interactive POSIX `sh` command line, run with the member repository as working directory, that terminates on its own (no watch modes, no prompts, no servers left running). The feature directory is exported as `$SPECIFY_FEATURE_DIRECTORY`, so checks can reference `$SPECIFY_FEATURE_DIRECTORY/contracts/`
     - Prefer commands scoped to what the feature changes when the full suite is prohibitively slow, but keep them deterministic
     - When no objective check can be derived, still write the heading with an HTML comment explaining why (`<!-- no automated checks: … -->`) — the member is then reported `skipped`, never `verified`
   - Optional YAML front matter `ready: false` (first lines of the file, between `---` markers) holds the member back from dispatch — together with every member that depends on it — so a feature can be dispatched in subsets. Write it only when the user asks for it; on re-runs keep whatever value is there

5a. **Write a contract-check task file when members share contracts.** When `FEATURE_DIR/contracts/` exists (or the plan defines an interface between members) and a producer member's output is consumed by another member, the cross-repo contract is verified by a **contract-check member**: an ordinary entry in `.specify/workspace.yml` whose `path` points to the consumer repository (or a dedicated contracts repository), with its own `tasks_file` and `depends_on` listing every producer:

   ```yaml
     - id: contract-check
       path: ../web                       # consumer repo, or a contracts repo
       tasks_file: tasks/contract-check.md
       depends_on: [api]                  # every producer of the contract
   ```

   - If the manifest already declares such a member, write its task file at `FEATURE_DIR/<member.tasks_file>`. If it does not, **do not edit the manifest yourself**: show the user the entry above (filled in) and write the task file at `FEATURE_DIR/tasks/contract-check.md` so it is ready once the entry is added
   - The task file contains **no task checkboxes** — only the header, a "Shared context" line pointing at `contracts/`, and the `## Verification` section. A task file with a Verification section and no checkboxes is *verification-only*: the orchestrator dispatches no agent for it, it just runs the commands after the producers' wave. Template:

     ```markdown
     # Contract check — <feature name>

     Producers: api · Consumer: web · Contracts: `contracts/` (feature directory)

     ## Verification

     - schema-diff: `npx @openapitools/openapi-diff "$SPECIFY_FEATURE_DIRECTORY/contracts/openapi.yaml" ../api/openapi.yaml` → exit 0
     - consumer-contract: `npx pact-verifier --provider-base-url http://localhost:8080 --pact-dir "$SPECIFY_FEATURE_DIRECTORY/contracts/pacts"` → exit 0
     ```

   - Commands validate **only contracts**: schema/IDL diffs between the feature's `contracts/` and what the producer actually exposes (OpenAPI, JSON Schema, protobuf/`buf breaking`, GraphQL, event schemas), and consumer-driven contract tests (Pact or similar) against the contract files. No implementation tasks, no unit tests of either side
   - Use the tools the repositories already have; paths to sibling repositories are relative to the contract-check member's `path`

6. **Rewrite `FEATURE_DIR/tasks.md` as the orchestration page**:
   - Keep the feature title and a two-line summary of what the feature delivers
   - Replace the task dump with a mirror table, one row per participating member:

     | Member | Task file | Depends on | Status |
     |--------|-----------|------------|--------|
     | s3 | tasks/s3.md | — | pending |
     | lambda | tasks/lambda.md | s3 | pending |

   - Generate the **Depends on** column from `.specify/workspace.yml` (`depends_on`), never guess it from the tasks — the manifest is the source of truth for dispatch order; this table is a human-readable mirror
   - Keep central/contract tasks listed under a separate "Central" section
   - Include a contract-check member (step 5a) in the table like any other member
   - Status column starts as `pending` for every member. The `multi-repo-implement` step keeps it current during dispatch (`pending → dispatched → verified | completed | failed`; `completed` means dispatched successfully with no Verification commands). Keep the column header exactly `Status` and the member id exactly as in the manifest so the step can find the row

7. **Report**: members detected vs participating, task counts per member, tasks left in the central slice, and any ambiguous classifications you resolved.

## Notes

- Re-running this command is safe: it regenerates member task files and the orchestration page, but **must preserve existing checkbox state** in member task files (append missing tasks, do not reset completed ones)
- Preserve the `## Verification` section the same way: keep every existing command line verbatim (including hand-edited commands and expected exit codes), append only checks that are missing, and never drop or weaken a check. Keep `ready:` front matter as is. In the orchestration page, keep each existing member's Status value (`dispatched`, `verified`, `failed`, …) instead of resetting it to `pending`
- If the feature genuinely touches a single repository, say so and suggest running the standard implement command instead — no split is needed
