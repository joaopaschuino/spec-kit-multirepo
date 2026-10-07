#!/usr/bin/env bash
# Idempotently maintain the multi-repo governance section in a repository's
# AGENTS.md. The section is the persistent channel that makes every agent
# session in a member repository aware of the workspace constitution layers —
# including free-form edits made after a dispatch, which bypass the
# speckit.multi-repo.implement command entirely.
#
# Usage: governance-note.sh <project-dir>
#
# The section is delimited by dedicated markers so it never collides with the
# agent-context extension's `<!-- SPECKIT START -->` managed block: re-running
# replaces the section in place and leaves all other content untouched.
set -euo pipefail

project="${1:?usage: governance-note.sh <project-dir>}"
cd "$project"

target="AGENTS.md"
start='<!-- SPECKIT-MULTI-REPO:GOVERNANCE START -->'
end='<!-- SPECKIT-MULTI-REPO:GOVERNANCE END -->'

section="$(mktemp)"
trap 'rm -f "$section"' EXIT
cat > "$section" <<'EOF'
<!-- SPECKIT-MULTI-REPO:GOVERNANCE START -->
## Multi-repo workspace governance

This repository is a member of a Spec Kit multi-repo workspace. When working
on a feature dispatched from the central specs repository (the directory in
`SPECIFY_FEATURE_DIRECTORY`, or `/speckit.multi-repo.implement`), every rule
in the workspace's constitution layers applies — including to later edits
that touch that feature's work, not only to the initial dispatch:

1. **Workspace** — `<central-root>/.specify/memory/constitution.md`
   (the central root is two levels above the feature directory)
2. **Member/type** — the file declared for this repository under
   `constitution:` in `<central-root>/.specify/workspace.yml`
3. **Local** — this repository's `.specify/memory/constitution.md`

When layers conflict, the most restrictive rule wins (each layer narrows the
one above, never loosens it). When a governance bundle path is passed as the
second argument of `/speckit.multi-repo.implement`, it assembles layers 1–3:
read it fully instead of re-resolving the paths.
<!-- SPECKIT-MULTI-REPO:GOVERNANCE END -->
EOF

if [ ! -f "$target" ]; then
  cp "$section" "$target"
elif grep -qF "$start" "$target"; then
  awk -v start="$start" -v end="$end" -v section="$section" '
    BEGIN { while ((getline line < section) > 0) sect[++n] = line }
    !replaced && index($0, start) {
      for (i = 1; i <= n; i++) print sect[i]
      replaced = 1; skip = 1; next
    }
    skip && index($0, end) { skip = 0; next }
    !skip { print }
  ' "$target" > "$target.tmp"
  mv "$target.tmp" "$target"
else
  # Append after making sure the file ends with a newline.
  [ -n "$(tail -c 1 "$target")" ] && printf '\n' >> "$target"
  printf '\n' >> "$target"
  cat "$section" >> "$target"
fi
