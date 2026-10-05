#!/usr/bin/env bash
# Build the release artifacts for a multi-repo version.
#
# Usage: ./make-release.sh [version]        (default: read from extension.yml)
#
# Produces dist/step-multi-repo-implement-<version>.zip — the custom step
# package asset to attach to the GitHub release (the extension installs
# straight from the tag archive; the step needs a dedicated archive because
# `specify workflow step add --from` expects step.yml at the archive root).
#
# After building, publish with:
#   git tag vX.Y.Z && git push origin vX.Y.Z
#   gh release create vX.Y.Z dist/step-multi-repo-implement-X.Y.Z.zip \
#     --title "vX.Y.Z" --notes-file CHANGELOG-excerpt
set -euo pipefail

root="$(cd "$(dirname "$0")" && pwd)"
cd "$root"

version="${1:-$(sed -n 's/^  version: "\(.*\)"/\1/p' extension.yml | head -1)}"
[ -n "$version" ] || { echo "error: cannot read version from extension.yml" >&2; exit 1; }
tag="v$version"

dist="dist"
stage="$dist/step-multi-repo-implement"
rm -rf "$stage"
mkdir -p "$stage"

cp -R step/multi-repo-implement/. "$stage/"
find "$stage" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true

artifact="$dist/step-multi-repo-implement-$tag.zip"
rm -f "$artifact"
python3 -m zipfile -c "$artifact" "$dist/step-multi-repo-implement"
rm -rf "$stage"

echo "built $artifact (attach it to the $tag GitHub release)"
