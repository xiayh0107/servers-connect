#!/bin/sh
# One-line installer for the SSH Server Manager Agent Skill (macOS / Linux).
#
#   curl -fsSL https://raw.githubusercontent.com/xiayh0107/servers-connect/main/install.sh | sh
#
# or from a checkout:  ./install.sh
#
# What it does:
#   1. Uses the checkout it runs from, or clones the repo to ~/.local/share/servers-connect
#   2. Creates the project venv and installs dependencies (scripts/bootstrap.py)
#   3. Symlinks the skill into every detected agent skills directory
#      (~/.claude/skills, ~/.codex/skills; add more via SSM_SKILLS_DIRS)
#   4. Runs `serverctl doctor` so problems surface immediately
#
# Overrides: SSM_REPO_URL, SSM_INSTALL_DIR, SSM_SKILLS_DIRS (space-separated).
set -eu

SKILL_NAME="ssh-server-manager"
REPO_URL="${SSM_REPO_URL:-https://github.com/xiayh0107/servers-connect.git}"
INSTALL_DIR="${SSM_INSTALL_DIR:-$HOME/.local/share/servers-connect}"

say() { printf '%s\n' "$*"; }
fail() { printf 'error: %s\n' "$*" >&2; exit 1; }

command -v python3 >/dev/null 2>&1 || fail "python3 (3.10+) is required"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
  || fail "python3 is older than 3.10"
command -v ssh >/dev/null 2>&1 || say "note: no ssh client on PATH; doctor will flag it"

# 1. Locate or fetch the source tree.
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" 2>/dev/null && pwd) || SCRIPT_DIR=""
if [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/$SKILL_NAME/SKILL.md" ]; then
  SOURCE="$SCRIPT_DIR/$SKILL_NAME"
  say "Using source checkout: $SOURCE"
elif [ -f "$INSTALL_DIR/$SKILL_NAME/SKILL.md" ]; then
  SOURCE="$INSTALL_DIR/$SKILL_NAME"
  if [ -d "$INSTALL_DIR/.git" ] && command -v git >/dev/null 2>&1; then
    say "Updating existing install in $INSTALL_DIR"
    git -C "$INSTALL_DIR" pull --ff-only || say "note: update skipped (local changes?)"
  fi
else
  command -v git >/dev/null 2>&1 || fail "git is required to fetch $REPO_URL"
  say "Cloning $REPO_URL -> $INSTALL_DIR"
  mkdir -p "$(dirname "$INSTALL_DIR")"
  git clone --depth 1 "$REPO_URL" "$INSTALL_DIR"
  SOURCE="$INSTALL_DIR/$SKILL_NAME"
fi

# 2. Dependencies (idempotent: reuses the venv when present).
say "Installing Python dependencies into $SOURCE/.venv ..."
python3 "$SOURCE/scripts/bootstrap.py" >/dev/null

# 3. Link into agent skills directories. Only agents that are actually
#    installed (their config dir exists) get a link, so this never scatters
#    directories for tools you don't use.
LINKED=""
set -- "$HOME/.claude/skills" "$HOME/.codex/skills"
if [ -n "${SSM_SKILLS_DIRS:-}" ]; then
  # shellcheck disable=SC2086
  set -- "$@" $SSM_SKILLS_DIRS
fi
for dir in "$@"; do
  parent=$(dirname "$dir")
  [ -d "$parent" ] || continue
  mkdir -p "$dir"
  target="$dir/$SKILL_NAME"
  if [ -e "$target" ] && [ ! -L "$target" ]; then
    say "skip: $target exists and is not a symlink"
    continue
  fi
  ln -sfn "$SOURCE" "$target"
  LINKED="$LINKED
  $target"
done

if [ -n "$LINKED" ]; then
  say "Skill linked into:$LINKED"
else
  say "No agent skills directory detected. Link manually, e.g.:"
  say "  ln -s \"$SOURCE\" ~/.claude/skills/$SKILL_NAME"
fi

# 3b. Put `serverctl` on PATH so humans and agents find it by name instead of
#     hand-rolling ssh when the skill directory is not the working directory.
BIN_DIR="$HOME/.local/bin"
if [ -d "$BIN_DIR" ]; then
  bin_target="$BIN_DIR/serverctl"
  if [ -e "$bin_target" ] && [ ! -L "$bin_target" ]; then
    say "skip: $bin_target exists and is not a symlink"
  else
    ln -sfn "$SOURCE/scripts/serverctl" "$bin_target"
    say "Command linked: serverctl -> $bin_target"
    case ":$PATH:" in
      *":$BIN_DIR:"*) ;;
      *) say "note: $BIN_DIR is not on PATH; add it to your shell profile" ;;
    esac
  fi
else
  say "note: create $BIN_DIR (and put it on PATH) to get a global 'serverctl' command"
fi

# 4. Health check.
say ""
say "Running serverctl doctor ..."
"$SOURCE/scripts/serverctl" doctor || {
  say "doctor reported issues above — see $SOURCE/docs/installation.md"
  exit 1
}
say ""
say "Done. Your agent can now use the '$SKILL_NAME' skill; humans can run:"
say "  $SOURCE/scripts/serverctl --help"
say "Re-run this installer any time to update: it pulls the latest release,"
say "re-links the skill, and 'serverctl doctor' warns when a linked copy goes stale."
