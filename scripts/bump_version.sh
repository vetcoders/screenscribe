#!/usr/bin/env bash
#
# Bump the project version end-to-end so the repo stays self-consistent:
#   1. pyproject.toml      -- the single source of truth for the version
#   2. CHANGELOG.md        -- promote the "## Unreleased" section to "## [x.y.z] - DATE"
#
# It deliberately does NOT reinstall: __version__ reads the *installed* package
# metadata (importlib.metadata), so `make install` is still required for
# `screenscribe --version` to report the new number and for `make verify`
# (test_version_metadata) to pass. That step is printed at the end.
#
# Usage: scripts/bump_version.sh <patch|minor|major>
set -euo pipefail

level="${1:-}"
case "$level" in
    patch | minor | major) ;;
    *)
        echo "usage: $(basename "$0") <patch|minor|major>" >&2
        exit 2
        ;;
esac

# Operate from the repo root regardless of where we are invoked from.
cd "$(dirname "$0")/.."

pyproject="pyproject.toml"
changelog="CHANGELOG.md"

cur="$(grep -m1 '^version = ' "$pyproject" | sed 's/version = "\(.*\)"/\1/')"
if [ -z "$cur" ]; then
    echo "error: could not read 'version = \"...\"' from $pyproject" >&2
    exit 1
fi

IFS='.' read -r major minor patch <<<"$cur"
# Drop any PEP 440 / SemVer suffix on the patch component (e.g. 13rc1, 13+g<sha>)
# so the arithmetic stays numeric instead of silently coercing.
patch="${patch%%[!0-9]*}"
if [ -z "${major}" ] || [ -z "${minor}" ] || [ -z "${patch}" ]; then
    echo "error: version '$cur' is not in MAJOR.MINOR.PATCH form" >&2
    exit 1
fi

case "$level" in
    patch) new="${major}.${minor}.$((patch + 1))" ;;
    minor) new="${major}.$((minor + 1)).0" ;;
    major) new="$((major + 1)).0.0" ;;
esac

echo "$cur -> $new"

# 1) pyproject.toml -- anchor the match to the start of line so we never touch
#    target-version / python_version or any other 'version' key.
sed "s/^version = \"${cur}\"/version = \"${new}\"/" "$pyproject" >"$pyproject.tmp"
mv "$pyproject.tmp" "$pyproject"
if ! grep -q "^version = \"${new}\"" "$pyproject"; then
    echo "error: pyproject version did not update to ${new}" >&2
    exit 1
fi

# 2) CHANGELOG.md -- promote the Unreleased section into a dated release heading,
#    keeping an empty "## Unreleased" placeholder on top for the next cycle.
date_str="$(date +%F)"
if grep -q '^## Unreleased' "$changelog"; then
    awk -v v="$new" -v d="$date_str" '
        !promoted && /^## Unreleased/ {
            print
            print ""
            print "## [" v "] - " d
            promoted = 1
            next
        }
        { print }
    ' "$changelog" >"$changelog.tmp"
    mv "$changelog.tmp" "$changelog"
    echo "CHANGELOG.md: promoted Unreleased -> [${new}] - ${date_str}"
else
    echo "WARN: no '## Unreleased' heading in $changelog -- add '## [${new}]' manually" >&2
fi

echo "Next: make install   # refresh installed metadata so 'screenscribe --version' == ${new} and 'make verify' passes"
