#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

PYTHON_BIN="${TMS_PYTHON:-python3.12}"
DIST_DIR="${DIST_DIR:-$REPO_ROOT/dist}"
INSTALL_VENV="${INSTALL_VENV:-$DIST_DIR/tms-env}"
ARCHIVE_PATH="${ARCHIVE_PATH:-$DIST_DIR/tms-env.tar.gz}"
CLEAN_FIRST="true"
BUILD_VENV_DIR="${BUILD_VENV_DIR:-$REPO_ROOT/.tmp-build-venv}"

usage() {
  cat <<'EOF'
Build the TMS package artifacts.

Usage:
  scripts/build_tms_package.sh [options]

Options:
  --python <path>         Python executable to use. Default: python3.12
  --dist-dir <path>       Output directory for built artifacts. Default: dist
  --install-venv <path>   Target runtime virtualenv root. Default: dist/tms-env
  --archive <path>        Runtime tar.gz path. Default: dist/tms-env.tar.gz
  --build-venv <path>     Temporary virtualenv used only for packaging tools
  --no-clean              Skip removing build/, dist/, and src/*.egg-info first
  --help                  Show this help

Examples:
  scripts/build_tms_package.sh
  scripts/build_tms_package.sh --python /opt/homebrew/bin/python3.12
  scripts/build_tms_package.sh --install-venv /usr/local/airflow/python3-virtualenv/tms-env
  scripts/build_tms_package.sh --install-venv dist/tms-env --archive dist/tms-env.tar.gz
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --python)
      PYTHON_BIN="${2:?missing value for --python}"
      shift 2
      ;;
    --dist-dir)
      DIST_DIR="${2:?missing value for --dist-dir}"
      shift 2
      ;;
    --install-venv)
      INSTALL_VENV="${2:?missing value for --install-venv}"
      shift 2
      ;;
    --archive)
      ARCHIVE_PATH="${2:?missing value for --archive}"
      shift 2
      ;;
    --build-venv)
      BUILD_VENV_DIR="${2:?missing value for --build-venv}"
      shift 2
      ;;
    --no-clean)
      CLEAN_FIRST="false"
      shift
      ;;
    --help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

command -v "$PYTHON_BIN" >/dev/null || {
  echo "Required Python executable not found: $PYTHON_BIN" >&2
  exit 1
}

"$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else "TMS packaging requires Python >= 3.12")'

if [[ "$CLEAN_FIRST" == "true" ]]; then
  rm -rf "$REPO_ROOT/build" "$DIST_DIR" "$REPO_ROOT"/src/*.egg-info
fi

mkdir -p "$DIST_DIR"

echo "Building TMS wheel with $PYTHON_BIN"
rm -rf "$BUILD_VENV_DIR"
"$PYTHON_BIN" -m venv "$BUILD_VENV_DIR"
"$BUILD_VENV_DIR/bin/python" -m pip install --upgrade pip build
"$BUILD_VENV_DIR/bin/python" -m build --wheel --outdir "$DIST_DIR" "$REPO_ROOT"

WHEEL_PATH="$(find "$DIST_DIR" -maxdepth 1 -type f -name 'type_materialisation_tools-*.whl' | sort | tail -n 1)"
if [[ -z "$WHEEL_PATH" ]]; then
  echo "Wheel build completed, but no wheel was found in $DIST_DIR" >&2
  exit 1
fi

echo "Built wheel:"
echo "  $WHEEL_PATH"

if [[ -n "$INSTALL_VENV" ]]; then
  if [[ ! -d "$INSTALL_VENV" ]]; then
    "$PYTHON_BIN" -m venv "$INSTALL_VENV"
  fi

  TARGET_PYTHON="$INSTALL_VENV/bin/python"
  if [[ ! -x "$TARGET_PYTHON" ]]; then
    echo "Target virtualenv Python not found: $TARGET_PYTHON" >&2
    exit 1
  fi

  echo "Installing wheel into:"
  echo "  $INSTALL_VENV"
  "$TARGET_PYTHON" -m pip install --upgrade "$WHEEL_PATH"
fi

if [[ -n "$ARCHIVE_PATH" ]]; then
  mkdir -p "$(dirname "$ARCHIVE_PATH")"
  rm -f "$ARCHIVE_PATH"
  tar -C "$(dirname "$INSTALL_VENV")" -czf "$ARCHIVE_PATH" "$(basename "$INSTALL_VENV")"

  echo "Built runtime archive:"
  echo "  $ARCHIVE_PATH"
fi
