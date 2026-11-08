# Deferred integration tests

These DB/Redis-backed tests predate the 0.1 prototype schema and are **not run by
default** (see docs/DESIGN-0.1.md §11). They will be revised after 0.1, together
with end-to-end, resilience and performance tests. To run whatever still applies:

    docker compose --profile test run --rm --build test python -m pytest tests/integration
