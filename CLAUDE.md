Always build and test using Docker (`docker compose up --build -d`). NEVER build locally with a virtual environment.
Run the test suite with `docker compose --profile test run --rm --build test`. PostgreSQL and Redis are on an internal compose network with no host ports; reach them with `docker compose exec db psql -U daash` / `docker compose exec redis redis-cli`.
Read README.md for project purpose. Never change README.md.
Read PLAN.md for project plan and current status. Always keep PLAN.md up-to-date.


