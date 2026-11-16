"""The embedder service (docs/DESIGN-0.4.1.md section 2): `uvicorn app.embedder_api:app`, port 8002.

The one process that holds an embedding model. Other containers call POST /embed; nothing is
published to the host. A background loop:
1. reads the model chosen in DAASHboard Settings (global_config "embedding") and loads it when it
   changes. If it cannot be loaded, the model in use stays (or, at the very start, the built-in
   model, else word hashes), and the error is shown in DAASHboard;
2. embeds every task, learning and memory that is not embedded by the model in use yet, newest
   first, in batches;
3. once none are left, keeps each table's HNSW index for that model and drops the others;
4. writes its state to `embedding_status` for DAASHboard.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any, Callable, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from app.config import Settings
from app.db import create_pool
from app.embed_models import Model, explain_load_error, load_model
from app.embeddings import EMBED_KINDS, EMBEDDED_TABLES, WORD_HASHES
from app.models import EmbeddingSetting
from app.repo import EmbeddingRepo

log = logging.getLogger(__name__)

MAX_TEXTS = 64
MAX_TEXT_CHARS = 20_000
BATCH = 32
#: batches per loop step, so a new setting is noticed while a large re-index runs
MAX_BATCHES_PER_STEP = 8

Loader = Callable[..., Model]


def _short(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}".strip()
    return text if len(text) <= 300 else text[:297] + "..."


class Embedder:
    """The loaded model, the indexer, and the status DAASHboard shows. One per service process."""

    def __init__(self, repo: EmbeddingRepo, settings: Any, loader: Loader = load_model) -> None:
        self.repo = repo
        self.settings = settings
        self.loader = loader
        self.model: Optional[Model] = None
        self.chosen: Optional[EmbeddingSetting] = None
        #: the setting that last failed to load; not retried until the user chooses again
        self.failed: Optional[EmbeddingSetting] = None
        self.error: Optional[str] = None
        #: (model, dim) whose HNSW indexes are in place
        self.indexed_for: Optional[tuple[str, int]] = None
        self.state = "starting"
        self._last_status: Optional[dict] = None
        self._encode_lock = asyncio.Lock()

    # -- embedding ------------------------------------------------------------------------------

    async def _encode(self, model: Model, texts: list[str], kind: str) -> list[list[float]]:
        async with self._encode_lock:   # one at a time: the model uses every core already
            return await asyncio.to_thread(model.encode, texts, kind)

    async def embed(self, texts: list[str], kind: str) -> tuple[Model, list[list[float]]]:
        """Raises LookupError while no model is loaded."""
        model = self.model
        if model is None:
            raise LookupError("no embedding model is loaded yet")
        return model, await self._encode(model, texts, kind)

    # -- the loop ---------------------------------------------------------------------------------

    async def _load(self, name: str, trust_remote_code: bool) -> Model:
        return await asyncio.to_thread(self.loader, name, trust_remote_code=trust_remote_code,
                                       settings=self.settings)

    async def _write_status(self, **fields: Any) -> None:
        fields.setdefault("chosen_model", self.chosen.model if self.chosen else None)
        fields.setdefault("active_model", self.model.name if self.model else None)
        fields.setdefault("dim", self.model.dim if self.model else None)
        fields.setdefault("error", self.error)
        if fields != self._last_status:
            await self.repo.set_status(**fields)
            self._last_status = fields

    async def ensure_model(self) -> None:
        setting = await self.repo.get_setting(self.settings.EMBEDDING_MODEL)
        self.chosen = setting
        current = self.model.name if self.model else None
        if setting.model == current:
            self.failed, self.error = None, None
            return
        if setting == self.failed:
            return
        self.state = "loading"
        await self._write_status(state="loading", indexed=0, total=0)
        try:
            model = await self._load(setting.model, setting.trust_remote_code)
        except Exception as exc:  # noqa: BLE001 - any download/load failure is shown, not fatal
            self.failed = setting
            self.error = explain_load_error(setting.model, exc)
            log.warning("could not load %s: %s", setting.model, _short(exc))
            if self.model is None:
                for name in dict.fromkeys((self.settings.EMBEDDING_MODEL, WORD_HASHES)):
                    if name == setting.model:
                        continue
                    try:
                        self.model = await self._load(name, False)
                        break
                    except Exception as exc2:  # noqa: BLE001
                        log.warning("fallback model %s did not load either: %s", name, _short(exc2))
            self.state = "error" if self.model is None else "indexing"
            await self._write_status(state=self.state, indexed=0, total=0)
            return
        log.info("embedding model %s loaded (%d dimensions)", model.name, model.dim)
        self.model, self.failed, self.error, self.indexed_for = model, None, None, None

    async def index(self) -> bool:
        """Embed rows not embedded by the model in use. True if more remain after this step."""
        model = self.model
        if model is None:
            return False
        done = 0
        for table in EMBEDDED_TABLES:
            while True:
                if done >= MAX_BATCHES_PER_STEP:
                    await self._progress(model)
                    return True
                rows = await self.repo.pending(table, model.name, BATCH)
                if not rows:
                    break
                if done == 0:
                    await self._progress(model)
                vectors = await self._encode(model, [t for _, t in rows], "document")
                await self.repo.store(table, model.name,
                                      [(rid, text, vec) for (rid, text), vec in zip(rows, vectors)])
                done += 1
        if self.indexed_for != (model.name, model.dim):
            await self.repo.ensure_indexes(model.name, model.dim)
            self.indexed_for = (model.name, model.dim)
        await self._progress(model)
        return False

    async def _progress(self, model: Model) -> None:
        counts = await self.repo.counts(model.name)
        total = sum(c["total"] for c in counts.values())
        indexed = sum(c["indexed"] for c in counts.values())
        ready = indexed >= total and self.indexed_for == (model.name, model.dim)
        self.state = "ready" if ready else "indexing"
        await self._write_status(state=self.state, indexed=indexed, total=total)

    async def step(self) -> bool:
        """One pass of the loop. True if there is more indexing to do right away."""
        await self.ensure_model()
        return await self.index()

    async def run(self, stop: asyncio.Event) -> None:
        poll = float(self.settings.EMBEDDER_POLL_SECONDS)
        delay = poll
        while not stop.is_set():
            try:
                more = await self.step()
                delay = 0.0 if more else poll
            except Exception:  # noqa: BLE001 - keep serving /embed; back off and retry
                log.exception("embedder step failed")
                delay = min(60.0, max(delay, poll) * 2)
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay or 0.01)
            except asyncio.TimeoutError:
                pass


# -- the app --------------------------------------------------------------------------------------

settings = Settings()
embedder: Optional[Embedder] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global embedder
    pool = None
    for attempt in range(1, 31):
        try:
            pool = await create_pool(settings.DATABASE_URL)
            break
        except Exception as exc:  # noqa: BLE001 - the database may still be starting
            log.warning("database not ready (%s); retry %d", exc, attempt)
            await asyncio.sleep(2)
    if pool is None:
        raise RuntimeError("could not connect to the database")
    embedder = Embedder(EmbeddingRepo(pool), settings)
    stop = asyncio.Event()
    task = asyncio.create_task(embedder.run(stop))
    log.info("Embedder ready (built-in models in %s, downloads in %s).",
             settings.EMBEDDING_BAKED_DIR, settings.EMBEDDING_CACHE_DIR)
    try:
        yield
    finally:
        stop.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await pool.close()


app = FastAPI(title="DAASH embedder", lifespan=lifespan, docs_url=None, redoc_url=None,
              openapi_url=None)


class EmbedRequest(BaseModel):
    texts: list[str] = Field(min_length=1, max_length=MAX_TEXTS)
    kind: str = "document"


@app.get("/health")
async def health() -> dict:
    e = embedder
    model = e.model if e else None
    return {"status": "ok", "service": "embedder", "state": e.state if e else "starting",
            "model": model.name if model else None, "dim": model.dim if model else None}


@app.post("/embed")
async def embed(req: EmbedRequest) -> dict:
    if req.kind not in EMBED_KINDS:
        raise HTTPException(422, f"kind must be one of {', '.join(EMBED_KINDS)}")
    if embedder is None:
        raise HTTPException(503, "the embedder is starting")
    try:
        model, vectors = await embedder.embed([t[:MAX_TEXT_CHARS] for t in req.texts], req.kind)
    except LookupError as exc:
        raise HTTPException(503, str(exc)) from None
    return {"model": model.name, "dim": model.dim, "vectors": vectors}
