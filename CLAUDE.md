Always build and test using Docker (`docker compose up --build -d`). NEVER build locally with a virtual environment.
Run the tests in three tiers (see docs/DESIGN-0.2.md): unit `docker compose --profile test run --rm --build test` (seconds, run on every change); integration `docker compose --profile integration run --rm --build integration` (real PostgreSQL/Redis, about a minute); e2e `scripts/e2e.sh` (real containers plus a scripted fake LLM, a few minutes, no published ports). PostgreSQL and Redis are on an internal compose network with no host ports; reach them with `docker compose exec db psql -U daash` / `docker compose exec redis redis-cli`.
External agent CLIs (OpenClaude, Claude Code, Codex) run through a runner: `docker compose --profile runner up --build -d runner` (needs `DAASH_RUNNER_BOOTSTRAP_TOKEN` in `.env`; see docs/RUNNER.md). The e2e tier uses runner containers with a fake CLI only, never the real CLIs or the gateway.
Read README.md for project purpose. Never change README.md.
Read PLAN.md for project plan and current status. Always keep PLAN.md up-to-date.


