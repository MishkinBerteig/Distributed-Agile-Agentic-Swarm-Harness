"""Loading embedding models inside the embedder service (docs/DESIGN-0.4.1.md section 2).

Only the `embedder` image has PyTorch and sentence-transformers; they are imported when a model is
loaded, never at import time, so the other images and the unit tests can import this module.

- `word-hashes` needs no model.
- A model built into the image is a plain folder, `<EMBEDDING_BAKED_DIR>/<org>__<name>`, saved with
  `SentenceTransformer.save` at build time (scripts/bake-embedding-model.py). It holds the model's own
  code too, so it loads with no network.
- Any other model is downloaded from Hugging Face into EMBEDDING_CACHE_DIR (a volume) on first use.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional, Protocol, Sequence

from app.embeddings import WORD_HASH_DIM, WORD_HASHES, trusts_remote_code, word_hash_vector

log = logging.getLogger(__name__)


class Model(Protocol):
    name: str
    dim: int

    def encode(self, texts: Sequence[str], kind: str) -> list[list[float]]: ...


class WordHashModel:
    name = WORD_HASHES
    dim = WORD_HASH_DIM

    def encode(self, texts: Sequence[str], kind: str) -> list[list[float]]:
        return [word_hash_vector(t) for t in texts]


def baked_folder(name: str, baked_dir: str) -> Path:
    """Where the image keeps a built-in model: one folder per model, '/' becoming '__'."""
    return Path(baked_dir) / name.replace("/", "__")


def baked_path(name: str, baked_dir: str) -> Optional[Path]:
    path = baked_folder(name, baked_dir)
    return path if (path / "modules.json").is_file() else None


class SentenceTransformerModel:
    """A sentence-transformers model on CPU. Vectors are L2-normalized; queries and documents use
    the model's own prompts when it has them (encode_query / encode_document)."""

    def __init__(self, name: str, *, baked_dir: str, cache_dir: str, trust_remote_code: bool,
                 max_seq_length: int = 512) -> None:
        from sentence_transformers import SentenceTransformer  # only in the embedder image

        self.name = name
        path = baked_path(name, baked_dir)
        if path is not None:
            log.info("loading built-in embedding model %s from %s", name, path)
            self._st: Any = SentenceTransformer(str(path), device="cpu", local_files_only=True,
                                                trust_remote_code=True)
        else:
            log.info("loading embedding model %s (downloading into %s if needed; own code %s)",
                     name, cache_dir, "allowed" if trust_remote_code else "not allowed")
            self._st = SentenceTransformer(name, device="cpu", cache_folder=cache_dir,
                                           trust_remote_code=trust_remote_code)
        get_dim = (getattr(self._st, "get_embedding_dimension", None)       # sentence-transformers 5.7+
                   or self._st.get_sentence_embedding_dimension)
        dim = get_dim()
        if not dim:
            raise ValueError(f"{name} does not say how long its vectors are; is it an embedding model?")
        self.dim = int(dim)
        if max_seq_length and (self._st.max_seq_length or 0) > max_seq_length:
            self._st.max_seq_length = max_seq_length

    def encode(self, texts: Sequence[str], kind: str) -> list[list[float]]:
        fn = self._st.encode_query if kind == "query" else self._st.encode_document
        arr = fn(list(texts), normalize_embeddings=True, batch_size=32, convert_to_numpy=True,
                 show_progress_bar=False)
        return [[float(x) for x in row] for row in arr]


def explain_load_error(name: str, exc: BaseException) -> str:
    """Why `name` did not load, in words DAASHboard can show (the raw error is logged)."""
    kind = type(exc).__name__
    text = str(exc)
    if kind == "GatedRepoError" or "gated repo" in text.lower():
        return (f"{name} is gated on Hugging Face: accept its terms there, then set HF_TOKEN in "
                f".env and restart the embedder.")
    if kind == "RepositoryNotFoundError" or "Repository Not Found" in text:
        return (f"Hugging Face has no public model called {name}. Check the name (a private model "
                f"needs HF_TOKEN in .env).")
    if "trust_remote_code" in text:
        return (f"{name} ships its own code. Allow it with \"Let this model run its own code\" if "
                f"you trust that code.")
    if kind in ("LocalEntryNotFoundError", "OfflineModeIsEnabled", "ConnectionError",
                "ConnectTimeout", "ReadTimeout") or "Max retries exceeded" in text:
        return f"Could not download {name}: the embedder could not reach Hugging Face."
    if isinstance(exc, ImportError):
        return "This embedder has no sentence-transformers, so it can only use word hashes."
    short = f"{kind}: {text}".strip()
    return f"Could not load {name}: " + (short if len(short) <= 300 else short[:297] + "...")


def load_model(name: str, *, trust_remote_code: bool, settings: Any) -> Model:
    """Load `name` (blocking: call it in a thread). Raises on any failure."""
    if name == WORD_HASHES:
        return WordHashModel()
    return SentenceTransformerModel(
        name, baked_dir=settings.EMBEDDING_BAKED_DIR, cache_dir=settings.EMBEDDING_CACHE_DIR,
        trust_remote_code=trusts_remote_code(name, trust_remote_code),
        max_seq_length=settings.EMBEDDING_MAX_SEQ_LENGTH)
