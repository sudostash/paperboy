#!/usr/bin/env bash
# Run the CI jobs from .github/workflows/ci.yml on this machine, before opening a PR.
#
#   scripts/ci-local.sh            # every job
#   scripts/ci-local.sh lint test-3.12 demos   # just these
#
# Jobs: lint, test-3.10 .. test-3.13 (with the 95% branch-coverage gate), test-mcp1,
# test-lowest (lowest dependency versions pyproject.toml allows) and demos (a real
# browser). Each job gets its own virtualenv under .ci/ (git-ignored), made with uv,
# which also downloads any Python version that isn't installed.
#
# Needs: uv (https://docs.astral.sh/uv/) and qpdf. LibreOffice is optional: without
# it, the conversion tests skip (CI installs it on Linux).
set -uo pipefail

cd "$(dirname "$0")/.."
UV="${UV:-$(command -v uv || true)}"
if [[ -z "$UV" ]]; then
  echo "uv is required: https://docs.astral.sh/uv/getting-started/installation/" >&2
  exit 2
fi
command -v qpdf >/dev/null || echo "warning: qpdf not found; tests that need it will skip" >&2
command -v soffice >/dev/null || command -v libreoffice >/dev/null \
  || echo "note: LibreOffice not found; conversion tests will skip (CI runs them)" >&2

ALL_JOBS=(lint test-3.10 test-3.11 test-3.12 test-3.13 test-mcp1 test-lowest demos)
JOBS=("${@:-${ALL_JOBS[@]}}")

# Make a fresh-enough env: create it once, then (re)install so dependency changes land.
env_for() {  # env_for <name> <python> <uv pip install args...>
  local dir=".ci/$1" python="$2"
  shift 2
  [[ -x "$dir/bin/python" ]] || "$UV" venv -q --python "$python" "$dir" || return 1
  VIRTUAL_ENV="$dir" "$UV" pip install -q --python "$dir/bin/python" "$@" || return 1
  echo "$dir/bin"
}

run_job() {
  local job="$1" bin
  case "$job" in
    lint)
      bin=$(env_for lint 3.12 -e ".[dev]") && "$bin/ruff" check . ;;
    test-3.1[0-3])
      bin=$(env_for "py${job#test-}" "${job#test-}" -e ".[dev]") &&
        "$bin/pytest" -q -p no:cacheprovider --cov=paperboy --cov-branch \
          --cov-report=term-missing:skip-covered --cov-fail-under=95 ;;
    test-mcp1)
      bin=$(env_for mcp1 3.12 -e ".[dev]" "mcp<2") && "$bin/pytest" -q -p no:cacheprovider ;;
    test-lowest)
      bin=$(env_for lowest 3.10 --resolution lowest-direct -e ".[dev]") &&
        "$bin/pytest" -q -p no:cacheprovider ;;
    demos)
      bin=$(env_for demos 3.12 -e ".[dev,demo]") &&
        "$bin/python" -m playwright install chromium >/dev/null &&
        "$bin/pytest" -q -p no:cacheprovider tests/test_demos.py ;;
    *)
      echo "unknown job: $job (jobs: ${ALL_JOBS[*]})" >&2; return 2 ;;
  esac
}

declare -a RESULTS
status=0
for job in "${JOBS[@]}"; do
  echo "=== $job"
  start=$SECONDS
  if run_job "$job"; then
    RESULTS+=("PASS  $job ($((SECONDS - start))s)")
  else
    RESULTS+=("FAIL  $job ($((SECONDS - start))s)")
    status=1
  fi
done

echo
echo "=== summary"
printf '%s\n' "${RESULTS[@]}"
exit $status
