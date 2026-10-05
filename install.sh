#!/usr/bin/env bash
# Install the multi-repo extension components into a Spec Kit project.
#
# Usage:
#   ./install.sh central <project-dir> [--release vX.Y.Z | --dev <package-dir>]
#   ./install.sh member <project-dir> [--release vX.Y.Z | --dev <package-dir>]
#
# "central" installs the agent commands, the custom workflow step, and the
# workflow definition into the central specs repository. "member" installs
# only the agent commands (used by member repositories).
#
# Sources:
#   --dev <dir>   local package directory (default: this script's directory)
#   --release V   GitHub release tag, e.g. v0.1.0 — installs from the
#                 published release artifacts
#
# Note: the custom step's `--from` install asks an interactive trust
# confirmation (default-deny) before downloading; answer "yes" when prompted.
set -euo pipefail

REPO="joaopaschuino/spec-kit-multirepo"

role="${1:?usage: install.sh central|member <project-dir> [--release vX.Y.Z | --dev <package-dir>]}"
project="${2:?usage: install.sh central|member <project-dir> [--release vX.Y.Z | --dev <package-dir>]}"
shift 2 || true

mode="dev"
src="$(cd "$(dirname "$0")" && pwd)"
release=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --dev) mode="dev"; shift; [ "$#" -gt 0 ] && { src="$(cd "$1" && pwd)"; shift; } ;;
    --release) mode="release"; release="${2:?--release requires a version tag}"; shift 2 ;;
    *) echo "error: unknown option: $1" >&2; exit 1 ;;
  esac
done

command -v specify >/dev/null 2>&1 || {
  echo "error: 'specify' CLI not found on PATH" >&2
  exit 1
}

project="$(cd "$project" && pwd)"
cd "$project"

if [ "$mode" = "dev" ]; then
  specify extension add "$src" --dev
  if [ "$role" = "central" ]; then
    specify workflow step add multi-repo-implement --dev "$src/step/multi-repo-implement"
    specify workflow add "$src/workflow/multi-repo" --dev
    specify workflow add "$src/workflow/multi-repo-check" --dev
  fi
else
  ext_zip="https://github.com/$REPO/archive/refs/tags/$release.zip"
  wf_url="https://raw.githubusercontent.com/$REPO/$release/workflow/multi-repo/workflow.yml"
  wf_check_url="https://raw.githubusercontent.com/$REPO/$release/workflow/multi-repo-check/workflow.yml"
  step_zip="https://github.com/$REPO/releases/download/$release/step-multi-repo-implement-$release.zip"
  specify extension add multi-repo --from "$ext_zip"
  if [ "$role" = "central" ]; then
    specify workflow step add multi-repo-implement --from "$step_zip"
    specify workflow add multi-repo --from "$wf_url"
    specify workflow add multi-repo-check --from "$wf_check_url"
  fi
fi

echo "multi-repo ($role, $mode) installed in $project"
