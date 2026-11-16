# DAASH 0.4.1: real embeddings for search

Status: built and verified for `v0.4.1` (2026-11-16). This extends docs/DESIGN-0.4.md.

## 1. Why

Up to 0.4, DAASH stored a vector for every task, learning and memory, and matched learnings and memories to each job
by pgvector cosine distance. But the Docker images had no embedding model. `DAASH_EMBEDDING_USE_MODEL=true` fell
back silently to word hashes, so "nearest" only meant "shares the most words". 0.4.1 makes the search real:

1. The default model, `Alibaba-NLP/gte-base-en-v1.5` (768 dimensions), is built into a Docker image and loads with
   no network.
2. DAASHboard Settings lets the user choose another embedding model.
3. PostgreSQL stores each vector with the model that made it and indexes the active model's vectors (HNSW).
4. Every search embeds its query with the same model and ranks by cosine distance in SQL.

## 2. The embedder service

A new container, `embedder` (image target `embedder`; `python -m app.embedder_api`, port 8002, internal only, no
published port). It is the only process that loads a model, so the model is in memory once, and the other images
stay small (no PyTorch).

- **Image.** `torch` (CPU wheels), `transformers==4.57.6`, `sentence-transformers==5.7.0`. transformers 5.x breaks
  gte-base-en-v1.5's own model code (its rotary `position_ids` come out uninitialised), so it is pinned to 4.57. The
  default model is saved with `SentenceTransformer.save` into `/opt/daash/embedding-models/<name>` at build time.
  That folder holds the model's own code too (`modeling.py`, `configuration.py`), so loading it never contacts
  Hugging Face. A trial showed 2 s to load it offline, about 950 MB of memory, and about 0.15 s to embed six short
  texts on CPU.
- **Other models** are downloaded from Hugging Face on first use into the volume `daash-models`
  (`/var/lib/daash/models`), so the service is also on the `frontend` network for egress. `HF_TOKEN` passes through
  for gated models.
- **API** (only other containers reach it):
  - `GET /health`: `{status, model, dim, state}`.
  - `POST /embed` `{texts: [...], kind: "query" | "document"}` returns `{model, dim, vectors}`. The service uses
    sentence-transformers' `encode_query` and `encode_document`, so models with query and document prompts (e5,
    nomic, mxbai...) get them. 503 while no model is ready.
- **Settings loop** (every 2 s, `DAASH_EMBEDDER_POLL_SECONDS`): reads the chosen model from `global_config` key `embedding`
  (`{model, trust_remote_code}`). When it differs from the loaded model, the service loads the new one in a thread.
  On success the new model becomes active. On failure the previous model stays active, and the error is shown in
  DAASHboard. It retries only when the setting changes.
- **Indexer** (same loop): rows of `tasks`, `learnings` and `memories` whose `embedding_model` is not the active
  model are re-embedded from their `embedding_text`, newest first, in batches of 32 (at most 8 batches per pass, so
  a new setting is noticed during a long re-index). When none are left, it makes sure the HNSW index for the active
  model exists on each table and drops the others. Progress and state go to the `embedding_status` row. Compose
  runs one embedder. A second one would only repeat work: each write is idempotent, and it only stores a vector if
  the row's text is unchanged.
- **Load errors** are shown in plain words: no such public model, a gated model (needs `HF_TOKEN`), a model that
  ships its own code (needs the checkbox), or no route to Hugging Face. The raw error goes to the log.
- **Word hashes** remain a model, named `word-hashes`. It needs no PyTorch, and the unit and integration tests use
  it in-process (no `DAASH_EMBEDDER_URL`). Users can also choose it in DAASHboard.

## 3. Clients

`app/embeddings.py`:
- `Embedding(text, model, vector)`: what a row stores. `vector` and `model` are `None` when the embedder could not
  be reached. The row then keeps its text, and the indexer embeds it later.
- `EmbedderClient` (`DAASH_EMBEDDER_URL`, async httpx, a 20 s timeout) and `LocalEmbedder` (word hashes,
  in-process). Both offer `await document(text)` and `await query(text)`, and neither raises: a failure gives an
  `Embedding` with no vector.
- Canonical texts: a task is embedded as `name\ndescription`, a memory as its content, and a learning as its
  subject (the mission, the task name or the learning itself, as before).

## 4. Schema (migration `f0a3c0d0e007`)

- `tasks`, `learnings`, `memories`:
  - `embedding` becomes `vector` with no fixed dimension, so models of any size fit;
  - new `embedding_model text` and `embedding_text text`;
  - the old word-hash vectors are kept and tagged `word-hashes`, with their text backfilled. The indexer then
    re-embeds them with the active model.
- `embedding_status` (one row): active model, dim, state (`starting | loading | indexing | ready | error`),
  indexed and total counts, error, chosen model, updated_at.
- HNSW indexes are per table and per model: partial expression indexes, created by the indexer:
  `CREATE INDEX <table>_emb_<key> ON <table> USING hnsw ((embedding::vector(D)) vector_cosine_ops) WHERE
  embedding_model = '<model>'`. pgvector cannot build an HNSW index above 2000 dimensions; such models are searched
  exactly, without an index.

## 5. Search

`LearningRepo.search` and `MemoryRepo.search` take the query `Embedding`:
1. Nearest rows of the query's model: `WHERE <scope> AND embedding_model = '<model>' ORDER BY
   embedding::vector(D) <=> $q::vector(D) LIMIT k`, inside a transaction with `SET LOCAL hnsw.iterative_scan =
   strict_order`. This keeps filtered HNSW scans exact and complete (pgvector 0.8).
2. If fewer than k come back (rows not indexed yet), the rest are the newest other rows, as before.

The model name goes into the SQL as a literal so the planner can match the partial index. Model names are
validated (`^[A-Za-z0-9][\w.-]*(/[A-Za-z0-9][\w.-]*)?$`) and quoted.

Searches that use this: every job's system prompt (the learnings and the agent's memories nearest the job's
subject), and the `recall` tool (built-in agents and every agent on the MCP server). Task vectors are kept current
but not searched yet.

## 6. DAASHboard

Settings gets a third card, **Search embeddings**:
- the state, in words: "Ready: gte-base-en-v1.5, 768 dimensions; 140 of 140 items indexed", "Downloading and
  loading …", "Re-indexing 40 of 140 …", or the error and which model is still in use;
- a choice of model from a short catalog, each with its size and dimensions, plus *Another Hugging Face model* (a
  name, and a checkbox to allow that model's own code, `trust_remote_code`);
- a note that a change downloads the model and re-indexes every task, learning and memory, and that until then the
  items not yet re-indexed are matched by recency.

The coordinator serves `GET /embeddings` (setting, status, catalog, counts) and `PUT /embeddings`.

## 7. Tests

- **Unit:** client fallbacks, model-name validation, the catalog, word hashes, the service API with a fake model,
  and the search SQL.
- **Integration:** the migration, mixed-model rows (768 and 384 dimensions) in one table, search without dimension
  errors, a model switch re-indexing everything and swapping the HNSW index, and the `/embeddings` endpoints.
- **e2e:** the real `embedder` container with the built-in model and no network. Its vectors rank by meaning, and
  rows written during a swarm run are indexed with gte-base-en-v1.5.
