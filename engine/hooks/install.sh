#!/bin/sh
#
# install.sh — point this repo's git hooks at scripts/vps/hooks/.
#
#   sh scripts/vps/hooks/install.sh              install
#   sh scripts/vps/hooks/install.sh --uninstall  remove
#   sh scripts/vps/hooks/install.sh --status     report what is currently configured
#
# It sets `core.hooksPath` rather than copying a file into .git/hooks. That matters: a
# copied hook is a SNAPSHOT. It stops tracking the version in the repo the moment either
# one changes, and nothing tells you. This repo's whole class of bug is "a rule recorded
# in one place while reality moved on somewhere else", so the hook is referenced, not
# duplicated — edit scripts/vps/hooks/pre-commit and every clone that ran this installer
# gets the change with its next `git pull`.
#
# core.hooksPath is local to the clone (it lives in .git/config, which is not committed),
# so each machine runs this once. Verify with --status.

set -eu

REPO_ROOT=$(git rev-parse --show-toplevel)
HOOKS_REL="scripts/vps/hooks"
HOOKS_ABS="$REPO_ROOT/$HOOKS_REL"

current() { git -C "$REPO_ROOT" config --local --get core.hooksPath 2>/dev/null || true; }

case "${1:-}" in
    --uninstall)
        if [ -n "$(current)" ]; then
            git -C "$REPO_ROOT" config --local --unset core.hooksPath
            echo "removed: core.hooksPath — git is back to .git/hooks (the default)"
        else
            echo "nothing to remove: core.hooksPath was not set"
        fi
        echo "The structural gates still run in CI and by hand:"
        echo "    python3 $HOOKS_REL/../check_wiring.py"
        exit 0
        ;;
    --status)
        cur=$(current)
        if [ "$cur" = "$HOOKS_REL" ]; then
            echo "INSTALLED — core.hooksPath = $cur"
            [ -x "$HOOKS_ABS/pre-commit" ] && echo "  pre-commit is executable" \
                                           || echo "  WARNING: pre-commit is NOT executable"
        elif [ -n "$cur" ]; then
            echo "OTHER      — core.hooksPath = $cur (not this repo's hooks)"
        else
            echo "NOT INSTALLED — core.hooksPath is unset; git uses .git/hooks"
        fi
        exit 0
        ;;
    "" ) : ;;
    -h|--help)
        sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'
        exit 0
        ;;
    * )
        echo "unknown option: $1  (try --help)" >&2
        exit 2
        ;;
esac

if [ ! -f "$HOOKS_ABS/pre-commit" ]; then
    echo "install failed: $HOOKS_ABS/pre-commit does not exist" >&2
    exit 1
fi

existing=$(current)
if [ -n "$existing" ] && [ "$existing" != "$HOOKS_REL" ]; then
    echo "WARNING: core.hooksPath is already set to '$existing'." >&2
    echo "         Overwriting it would silently disable whatever that provides." >&2
    echo "         Merge the two hook directories by hand, then re-run this." >&2
    exit 1
fi

chmod +x "$HOOKS_ABS/pre-commit" 2>/dev/null || true
git -C "$REPO_ROOT" config --local core.hooksPath "$HOOKS_REL"

echo "installed: core.hooksPath = $HOOKS_REL"
echo
echo "Every commit now runs three structural gates (wiring, send path, board writes)."
echo "Verify right now, without committing anything:"
echo "    python3 scripts/vps/check_wiring.py"
echo
echo "Deliberate bypass, one commit at a time:   git commit --no-verify"
echo "Remove entirely:                           sh $HOOKS_REL/install.sh --uninstall"
