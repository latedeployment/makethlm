#!/usr/bin/env bash
set -euo pipefail

VERSION=$(uv run python -c "import makethlm; print(makethlm.__version__)")
DIST_DIR="dist"

usage() {
    echo "Usage: $0 [--test] [--skip-tests] [--clean-only] [--no-publish] [--validate]"
    echo ""
    echo "Build and publish makethlm ${VERSION} to PyPI."
    echo ""
    echo "Options:"
    echo "  --test        Upload to TestPyPI instead of PyPI"
    echo "  --skip-tests  Skip running the test suite before building"
    echo "  --clean-only  Only clean build artifacts, don't build or upload"
    echo "  --no-publish  Build only, skip the upload step"
    echo "  --validate    Install the built wheel in a temp venv and smoke-test it"
    echo "  -h, --help    Show this help message"
    echo ""
    echo "Authentication:"
    echo "  Set your PyPI API token via one of these methods (in priority order):"
    echo ""
    echo "  1. Environment variable:"
    echo "       export UV_PUBLISH_TOKEN=pypi-xxxxxxxx"
    echo ""
    echo "  2. Pass directly:"
    echo "       UV_PUBLISH_TOKEN=pypi-xxxxxxxx $0"
    echo ""
    echo "  3. Username/password env vars (for legacy tokens):"
    echo "       export UV_PUBLISH_USERNAME=__token__"
    echo "       export UV_PUBLISH_PASSWORD=pypi-xxxxxxxx"
    echo ""
    echo "  Generate a token at:"
    echo "    PyPI:     https://pypi.org/manage/account/token/"
    echo "    TestPyPI: https://test.pypi.org/manage/account/token/"
    exit 0
}

TARGET="pypi"
SKIP_TESTS=false
CLEAN_ONLY=false
NO_PUBLISH=false
VALIDATE=false

for arg in "$@"; do
    case "$arg" in
        --test)        TARGET="testpypi" ;;
        --skip-tests)  SKIP_TESTS=true ;;
        --clean-only)  CLEAN_ONLY=true ;;
        --no-publish)  NO_PUBLISH=true ;;
        --validate)    VALIDATE=true; NO_PUBLISH=true ;;
        -h|--help)     usage ;;
        *)             echo "Unknown option: $arg"; usage ;;
    esac
done

# --- Check tools ---
if ! command -v uv &>/dev/null; then
    echo "ERROR: 'uv' not found. Install it: https://docs.astral.sh/uv/getting-started/installation/"
    exit 1
fi

# --- Clean ---
echo "==> Cleaning old build artifacts..."
rm -rf "$DIST_DIR" build *.egg-info makethlm/*.egg-info
echo "    Done."

if $CLEAN_ONLY; then
    echo "==> Clean complete."
    exit 0
fi

# --- Tests ---
if ! $SKIP_TESTS; then
    echo "==> Running tests..."
    uv run ruff check .
    uv run ruff format --check .
    uv run pytest tests/ -q --no-docker
    echo "    Tests passed."
else
    echo "==> Skipping tests (--skip-tests)."
fi

# --- Build ---
echo "==> Building wheel for makethlm ${VERSION}..."
uv build --wheel
echo "    Built:"
ls -lh "$DIST_DIR"/

# --- Validate ---
if $VALIDATE; then
    WHEEL=$(ls "$DIST_DIR"/*.whl | head -1)
    TMPVENV=$(mktemp -d)
    trap 'rm -rf "$TMPVENV"' EXIT

    echo "==> Validating package in a fresh venv..."
    echo "    Creating temp venv at $TMPVENV"
    uv venv "$TMPVENV" --quiet
    VENV_PYTHON="$TMPVENV/bin/python"

    echo "    Installing $WHEEL"
    uv pip install --python "$VENV_PYTHON" "$WHEEL" --quiet

    echo "    Checking import..."
    "$VENV_PYTHON" -c "import makethlm; print(f'    makethlm {makethlm.__version__} imported OK')"

    echo "    Checking CLI entry point..."
    "$TMPVENV/bin/makethlm" --help > /dev/null
    echo "    makethlm --help OK"

    echo "    Checking parse round-trip..."
    "$VENV_PYTHON" -c "
from makethlm.parser import parse
pf = parse('task hello:\n    say hi\n')
assert 'hello' in pf.tasks, 'task not found'
assert pf.tasks['hello'].prompt == 'say hi', 'prompt mismatch'
print('    parse round-trip OK')
"

    echo ""
    echo "==> Validation passed. Package is good."
    exit 0
fi

# --- Upload ---
if $NO_PUBLISH; then
    echo "==> Skipping upload (--no-publish). Packages are in $DIST_DIR/."
    exit 0
fi

if [ "$TARGET" = "testpypi" ]; then
    echo "==> Uploading to TestPyPI..."
    uv publish --publish-url https://test.pypi.org/legacy/
    echo ""
    echo "    Uploaded to TestPyPI. Install with:"
    echo "    uv pip install --index-url https://test.pypi.org/simple/ makethlm"
else
    echo ""
    echo "==> Ready to upload makethlm ${VERSION} to PyPI."
    read -rp "    Proceed? [y/N] " confirm
    if [[ "$confirm" =~ ^[Yy]$ ]]; then
        uv publish
        echo ""
        echo "    Published! Install with:"
        echo "    uv pip install makethlm"
    else
        echo "    Upload cancelled. Packages are in $DIST_DIR/."
    fi
fi
