#!/bin/sh
set -eu

REPO_URL="${MMUX_REPO_URL:-https://github.com/hanaukyo47/mmux.git}"
BRANCH="${MMUX_BRANCH:-main}"
INSTALL_ROOT="${MMUX_INSTALL_ROOT:-${XDG_DATA_HOME:-$HOME/.local/share}/mmux}"
BIN_DIR="${MMUX_BIN_DIR:-$HOME/.local/bin}"
REPO_DIR="$INSTALL_ROOT/repo"
VENV_DIR="$INSTALL_ROOT/venv"
BIN_PATH="$BIN_DIR/mmux"
FORCE="${MMUX_FORCE:-0}"
INSTALL_DEPS="${MMUX_INSTALL_DEPS:-0}"

say() {
  printf '%s\n' "$*"
}

warn() {
  printf 'warning: %s\n' "$*" >&2
}

fail() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

need() {
  command -v "$1" >/dev/null 2>&1 || fail "$1 is required"
}

is_root() {
  [ "$(id -u)" = "0" ]
}

python_bin() {
  if command -v python3 >/dev/null 2>&1; then
    command -v python3
  elif command -v python >/dev/null 2>&1; then
    command -v python
  else
    fail "python3 is required"
  fi
}

ensure_tmux() {
  if command -v tmux >/dev/null 2>&1; then
    return
  fi

  if [ "$INSTALL_DEPS" = "1" ] && command -v brew >/dev/null 2>&1; then
    say "Installing tmux with Homebrew..."
    brew install tmux
    return
  fi

  if [ "$INSTALL_DEPS" = "1" ] && command -v apt-get >/dev/null 2>&1 && is_root; then
    say "Installing tmux with apt-get..."
    apt-get update
    apt-get install -y tmux
    return
  fi

  warn "tmux is not installed. Install it before running mmux tmux workspaces."
  warn "On macOS with Homebrew: brew install tmux"
  warn "On Ubuntu/Debian: apt-get install -y tmux"
  warn "Or rerun with MMUX_INSTALL_DEPS=1 where Homebrew is available or apt-get is running as root."
}

install_repo() {
  mkdir -p "$INSTALL_ROOT"

  if [ -d "$REPO_DIR/.git" ]; then
    say "Updating mmux source in $REPO_DIR..."
    git -C "$REPO_DIR" fetch origin "$BRANCH"
    git -C "$REPO_DIR" checkout "$BRANCH"
    git -C "$REPO_DIR" pull --ff-only origin "$BRANCH"
  elif [ -e "$REPO_DIR" ]; then
    fail "$REPO_DIR exists but is not a git repository"
  else
    say "Cloning mmux into $REPO_DIR..."
    git clone --branch "$BRANCH" --depth 1 "$REPO_URL" "$REPO_DIR"
  fi
}

install_python_package() {
  py="$(python_bin)"
  say "Creating virtual environment in $VENV_DIR..."
  if ! "$py" -m venv "$VENV_DIR"; then
    fail "could not create a Python virtual environment. On Ubuntu/Debian install python3-venv, then rerun this installer."
  fi
  "$VENV_DIR/bin/python" -m pip install --upgrade pip setuptools >/dev/null
  "$VENV_DIR/bin/python" -m pip install -e "$REPO_DIR"
}

link_binary() {
  mkdir -p "$BIN_DIR"

  if [ -e "$BIN_PATH" ] && [ ! -L "$BIN_PATH" ] && [ "$FORCE" != "1" ]; then
    fail "$BIN_PATH already exists and is not a symlink. Set MMUX_FORCE=1 to replace it."
  fi

  ln -sfn "$VENV_DIR/bin/mmux" "$BIN_PATH"
}

print_done() {
  say ""
  say "mmux installed:"
  say "  $BIN_PATH"

  case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *)
      say ""
      warn "$BIN_DIR is not in PATH."
      say "Add this to your shell profile:"
      say "  export PATH=\"$BIN_DIR:\$PATH\""
      say "For this shell, run:"
      say "  export PATH=\"$BIN_DIR:\$PATH\""
      say "Or call mmux directly as:"
      say "  $BIN_PATH"
      ;;
  esac

  say ""
  say "Try:"
  say "  mmux doctor"
}

need git
ensure_tmux
install_repo
install_python_package
link_binary
print_done
