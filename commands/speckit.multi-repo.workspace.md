---
description: Scaffold, audit, or bootstrap (clone the missing members of) the multi-repo workspace manifest (.specify/workspace.yml) against the local filesystem
---

## User Input

```text
$ARGUMENTS
```

The user may pass `audit` to force the audit mode, `bootstrap` (or a request such as "configure this", "set up the workspace", "clone the members") for the bootstrap mode, or a directory name to add as a new member.

## Purpose

This repository is the **central specs repository** of a multi-repo workspace. This command maintains `.specify/workspace.yml` — the manifest declaring the workspace's member repositories and their dependency graph. It has three modes:

- **Scaffold** (no manifest yet): discover Spec Kit projects around this repository and propose a manifest. Dependency knowledge is human: the command proposes members, **never invents `depends_on`**.
- **Audit** (manifest exists): report member readiness and drift (repos present locally but not declared), propose additions with user confirmation, and fill missing member `url`s from their `origin` remote. Never modifies any other field of existing entries.
- **Bootstrap** (manifest exists, `bootstrap` requested): clone the members missing locally from their manifest `url`. This is the "I just cloned the central repository — configure the workspace" path.

## Mode 1 — Scaffold (no manifest yet)

1. Verify `.specify/workspace.yml` does not exist. If it exists, go to Mode 2.

2. **Discover candidate members**: list the entries of the parent directory of the repo root (`ls ..`), and for each directory other than this repository, test whether `<name>/.specify` exists (e.g. `test -d "../<name>/.specify"`). Also check subdirectories of the repo root up to two levels deep (`<dir>/.specify` and `<dir>/<subdir>/.specify`, e.g. `apps/web` in a mono-repo, or submodule layouts), skipping `.git`, `.specify`, `specs`, and dependency/build directories such as `node_modules`, `vendor`, `.venv`. Each hit is a Spec Kit project — a candidate member. If none are found, report that no sibling Spec Kit projects were found, show a minimal manifest template, and stop.

3. **Propose the manifest**: build a table of candidates — directory name, relative path from the repo root, and whether it looks like a code repo (has `.git`, source files) or a specs repo. Propose this YAML:

   ```yaml
   schema_version: "1.0"
   workspace:
     id: <slug-of-the-platform>        # ask the user, or derive from the central repo's directory name
     name: "<Human name>"
   members:
     - id: <member-id>                 # unique; the task file defaults to tasks/<id>.md
       path: <relative-path>
       url: <origin-remote>            # only when the member has its own 'origin' (see below)
       # constitution: constitutions/<type>.md   # optional shared governance file
       # domain: <business-domain>                # optional; groups members by domain
       # communicates_with: []                    # optional; members it talks to at runtime (may be bidirectional)
       depends_on: []                  # TODO: fill in — who must complete before this member?
   ```

   **Member ids** must be unique and match `^[a-z0-9][a-z0-9-]*$`: use the directory name, lowercased, with any other character replaced by `-`. When two or more candidates would get the same id (e.g. `apps/web` and `services/web`), give each of them an id derived from its relative path instead (`apps-web`, `services-web`); use these ids consistently in `depends_on` and `communicates_with`.

   **Member `url`**: when the candidate is the top level of its own git repository (`git -C <path> rev-parse --show-toplevel` resolves to the candidate path) and `git -C <path> config --get-all remote.origin.url` succeeds, set `url` to the first value it prints (the one git fetches from) — it lets teammates clone the member with the bootstrap. The bootstrap runs `git clone` from the central repository root, so when that value is a relative local path (no `scheme://`, not `host:path`), resolve it against the member path and write the absolute path instead. Omit `url` for mono-repo subdirectories (their `origin` is this repository's) and for repositories without an `origin`.

   Every member starts with `depends_on: []`. `depends_on` is the **execution order** (acyclic); `domain` and `communicates_with` describe the **domain topology** (communication may be bidirectional, so cycles are allowed there) and never affect dispatch.

4. **Confirm with the user** (interactive): show the proposal, ask which candidates to include, and ask — per member — which other members it depends on (the graph only the team knows) and, optionally, its domain and which members it communicates with at runtime. Record their answers; never invent any of them.

5. **Write** `.specify/workspace.yml` with the confirmed members and dependencies. If running **headless** (no user to answer): write the manifest with empty `depends_on` for every member and print a prominent warning that the dependency graph is empty (all members would dispatch in wave 0) and must be filled before the first dispatch is trusted.

6. **Report next steps**: run `/speckit.multi-repo.domain` to map the domain topology (`domain`, `communicates_with`) from the members' code, `/speckit.multi-repo.tasks` after a feature's tasks exist, and `specify workflow run multi-repo-check` to verify member readiness at any time.

## Mode 2 — Audit (manifest exists)

1. Read `.specify/workspace.yml` (same checks as the orchestrator: schema, ids, known dependencies, acyclic `depends_on` graph, `communicates_with` referencing known members other than itself — cycles there are allowed). If it is invalid, report the parser error and stop.

2. **Check each declared member** and build a status table:

   | Member | Cloned | Initialized | Integration | Constitution | URL | Status |
   |--------|--------|-------------|-------------|--------------|-----|--------|
   | itau-nm8-lambda-integracao | yes | yes | claude | constitutions/lambda.md ✓ | git@github.com:itau/nm8-lambda.git (suggested) | ready |
   | itau-nm8-glue-etl | **no** | — | — | — | git@github.com:itau/nm8-glue.git | not cloned — run bootstrap |

   - *Cloned*: the member `path` exists on disk
   - *Initialized*: `<path>/.specify` exists
   - *Integration*: `integration` in the manifest, or `<path>/.specify/integration.json`
   - *Constitution*: the manifest's `constitution` file exists in this repository
   - *URL*: the manifest's `url`; when it is missing and the member exists, the member's `origin` remote marked *(suggested)* — only if the member path is the top level of its own git repository (same rule as Mode 1; never for mono-repo subdirectories)
   - Map failures to statuses: not cloned / not initialized / no integration / constitution missing

3. **Detect drift**: repeat the Mode 1 discovery scan; list Spec Kit projects found by that scan that are **not** declared in the manifest as "undeclared candidates".

4. **Report**: the table above, a note to run the bootstrap (Mode 3) when any member is not cloned but has a `url`, undeclared candidates, a **topology** section when any member declares `domain` or `communicates_with` — a Mermaid `flowchart LR` with one `subgraph` per domain, `dep ==> member` for each `depends_on` edge (execution order), `a -.-> b` for each `communicates_with` edge (`a <-.-> b` when both list each other) — and a note that `specify workflow run multi-repo-check` gives the same readiness report (plus dispatch waves) in machine-readable form via `--json`.

5. **Additions need confirmation**: if the user asked to add a member (or approves an undeclared candidate), append the entry with `depends_on: []` (its id follows the Mode 1 rules and must not collide with an existing id — derive it from the relative path if it would) and flag that the dependency must be set by hand. **Never** modify or remove existing members or their `depends_on` in this mode.

6. **Fill missing `url`s** (confirmation required): list the *(suggested)* URLs and, once the user confirms, add `url: <origin>` to those existing entries — the only change this mode makes to an existing entry; never overwrite an `url` that is already declared. Headless: report the suggestions and write nothing.

## Mode 3 — Bootstrap (clone the missing members)

Runs when the user asks for `bootstrap` (or to configure / set up the workspace after cloning only this repository). Requires `.specify/workspace.yml`; if it is missing, say so and go to Mode 1.

1. Run `specify workflow run multi-repo-bootstrap -i dry_run=true` and show which members would be cloned, which are already present, and which are absent without a `url`. Interactively, confirm before cloning; headless, proceed.

2. Run `specify workflow run multi-repo-bootstrap`. For each member whose `path` does not exist and that declares `url`, it runs `git clone <url> <path>` (parent directories created; local paths in `url` are relative to this repository). Members already present are skipped; absent members without `url` are reported and skipped; a failed clone aborts the run with the git error (clones completed before it are kept). Clones that land inside this repository (e.g. `apps/web`) are added to its `.gitignore` so they are never recorded as embedded repositories. Credential prompts are disabled — the clone must authenticate through an SSH agent or a credential helper.

   If the `multi-repo-bootstrap` workflow is not installed (older install), follow the same rules by hand with `git clone`, and suggest re-running `install.sh central` to get it.

3. **Report** the cloned / present / missing members and the `.gitignore` entries added. For absent members without `url`, ask the user for the remote (then add `url` to that entry, with confirmation, and re-run). For cloned members that are not Spec Kit projects yet (`initialized: false`), point to `specify init --here --integration <agent>` and `install.sh member`. Finish with `specify workflow run multi-repo-check` to confirm every member is ready.
