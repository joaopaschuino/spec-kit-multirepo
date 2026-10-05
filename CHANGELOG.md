# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
