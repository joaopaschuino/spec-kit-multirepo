---
description: Scaffold or audit the multi-repo workspace manifest (.specify/workspace.yml) against the local filesystem
---

## User Input

```text
$ARGUMENTS
```

The user may pass `audit` to force the audit mode, or a directory name to add as a new member.

## Purpose

This repository is the **central specs repository** of a multi-repo workspace. This command maintains `.specify/workspace.yml` — the manifest declaring the workspace's member repositories and their dependency graph. It has two modes:

- **Scaffold** (no manifest yet): discover Spec Kit projects around this repository and propose a manifest. Dependency knowledge is human: the command proposes members, **never invents `depends_on`**.
- **Audit** (manifest exists): report member readiness and drift (repos present locally but not declared), and propose additions with user confirmation. Never modifies existing entries.

## Mode 1 — Scaffold (no manifest yet)

1. Verify `.specify/workspace.yml` does not exist. If it exists, go to Mode 2.

2. **Discover candidate members**: list the entries of the parent directory of the repo root (`ls ..`), and for each directory other than this repository, test whether `<name>/.specify` exists (e.g. `test -d "../<name>/.specify"`). Also check subdirectories of the repo root the same way (submodule layouts). Each hit is a Spec Kit project — a candidate member. If none are found, report that no sibling Spec Kit projects were found, show a minimal manifest template, and stop.

3. **Propose the manifest**: build a table of candidates — directory name, relative path from the repo root, and whether it looks like a code repo (has `.git`, source files) or a specs repo. Propose this YAML:

   ```yaml
   schema_version: "1.0"
   workspace:
     id: <slug-of-the-platform>        # ask the user, or derive from the central repo's directory name
     name: "<Human name>"
   members:
     - id: <directory-name>            # member id; the task file defaults to tasks/<id>.md
       path: <relative-path>
       # constitution: constitutions/<type>.md   # optional shared governance file
       depends_on: []                  # TODO: fill in — who must complete before this member?
   ```

   Every member starts with `depends_on: []`.

4. **Confirm with the user** (interactive): show the proposal, ask which candidates to include, and ask — per member — which other members it depends on (the graph only the team knows). Record their answers.

5. **Write** `.specify/workspace.yml` with the confirmed members and dependencies. If running **headless** (no user to answer): write the manifest with empty `depends_on` for every member and print a prominent warning that the dependency graph is empty (all members would dispatch in wave 0) and must be filled before the first dispatch is trusted.

6. **Report next steps**: run `/speckit.multi-repo.tasks` after a feature's tasks exist, and `specify workflow run multi-repo-check` to verify member readiness at any time.

## Mode 2 — Audit (manifest exists)

1. Read `.specify/workspace.yml` (same checks as the orchestrator: schema, ids, known dependencies, acyclic graph). If it is invalid, report the parser error and stop.

2. **Check each declared member** and build a status table:

   | Member | Cloned | Initialized | Integration | Constitution | Status |
   |--------|--------|-------------|-------------|--------------|--------|
   | itau-nm8-lambda-integracao | yes | yes | claude | constitutions/lambda.md ✓ | ready |
   | itau-nm8-glue-etl | **no** | — | — | — | not cloned |

   - *Cloned*: the member `path` exists on disk
   - *Initialized*: `<path>/.specify` exists
   - *Integration*: `integration` in the manifest, or `<path>/.specify/integration.json`
   - *Constitution*: the manifest's `constitution` file exists in this repository
   - Map failures to statuses: not cloned / not initialized / no integration / constitution missing

3. **Detect drift**: repeat the Mode 1 discovery scan; list sibling Spec Kit projects **not** declared in the manifest as "undeclared candidates".

4. **Report**: the table above, undeclared candidates, and a note that `specify workflow run multi-repo-check` gives the same readiness report (plus dispatch waves) in machine-readable form via `--json`.

5. **Additions need confirmation**: if the user asked to add a member (or approves an undeclared candidate), append the entry with `depends_on: []` and flag that the dependency must be set by hand. **Never** modify or remove existing members or their `depends_on` in this mode.
