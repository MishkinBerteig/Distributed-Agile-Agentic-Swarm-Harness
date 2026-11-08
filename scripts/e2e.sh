#!/usr/bin/env bash
# End-to-end tier (docs/DESIGN-0.2.md A.3): the real containers plus a fake-LLM container, in a
# separate compose project with no published ports, so it can run beside the dev stack.
#
#   scripts/e2e.sh                       # project name daash-e2e
#   DAASH_E2E_PROJECT=mine scripts/e2e.sh
#
# Exits with the status of the tests; always tears the project down (containers, network and
# volumes of THIS project only).
#
# Why not `up --abort-on-container-exit --exit-code-from e2e`? The restart scenario restarts the
# engine container through the Docker API, and Compose treats any container stopping as "a
# container exited" and tears the whole project down mid-test. So the stack is started detached
# and waited for (`up -d --wait`), and the tests run as a one-off `run` whose exit code is ours.
set -u
cd "$(dirname "$0")/.."

PROJECT="${DAASH_E2E_PROJECT:-daash-e2e}"
export DAASH_E2E_PROJECT="$PROJECT"
COMPOSE=(docker compose -p "$PROJECT" -f docker-compose.e2e.yaml)

cleanup() {
  "${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1
}
trap 'cleanup; exit 130' INT TERM

status=0
"${COMPOSE[@]}" up -d --build --wait db redis fake-llm coordinator engine || status=$?
if [ "$status" -eq 0 ]; then
  "${COMPOSE[@]}" run --rm --no-deps --build e2e
  status=$?
fi

if [ "$status" -ne 0 ]; then
  echo "--- e2e failed (exit $status); last service log lines ---" >&2
  "${COMPOSE[@]}" logs --tail=40 engine coordinator fake-llm 2>&1 >&2 || true
fi
"${COMPOSE[@]}" down -v --remove-orphans
exit "$status"
