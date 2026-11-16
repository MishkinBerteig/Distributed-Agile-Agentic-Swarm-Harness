#!/usr/bin/env python3
"""Build step of the embedder image (Dockerfile, target `embedder-deps`; docs/DESIGN-0.4.1.md section 2).

    python bake-embedding-model.py <hugging face model id> <folder>

Downloads the model and saves it with SentenceTransformer.save as a plain folder,
<folder>/<org>__<name>. The folder holds the model's own code too (gte-base-en-v1.5 has some), so
app/embed_models.py loads it with no network. The Hugging Face download cache is deleted. Then it
loads the saved folder once with the network turned off and checks that it embeds.
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path


def main() -> int:
    name, out_dir = sys.argv[1], sys.argv[2]
    folder = Path(out_dir) / name.replace("/", "__")
    scratch = tempfile.mkdtemp(prefix="hf-")
    os.environ["HF_HOME"] = scratch
    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(name, trust_remote_code=True, cache_folder=scratch, device="cpu")
    model.save(str(folder))
    shutil.rmtree(scratch, ignore_errors=True)

    check = SentenceTransformer(str(folder), trust_remote_code=True, local_files_only=True,
                                device="cpu")
    vec = check.encode_document(["DAASH bakes its embedding model into the image."])
    dim = (getattr(check, "get_embedding_dimension", None) or check.get_sentence_embedding_dimension)()
    assert vec.shape == (1, dim), vec.shape
    print(f"baked {name} into {folder} ({dim} dimensions)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
