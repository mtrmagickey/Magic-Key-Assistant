#!/usr/bin/env python3
"""Re-embed all ChromaDB chunks with the local Ollama embedding model.

This script:
  1. Backs up the current Chroma DB directory.
  2. Reads every chunk (text + metadata) from the existing collection.
  3. Deletes the old collection.
  4. Re-creates it using Ollama embeddings (nomic-embed-text by default).
  5. Inserts all chunks in batches, preserving every chunk ID and all metadata.

Usage:
    cd LeisureLLM
    python scripts/reembed_local.py [--batch-size 50] [--model nomic-embed-text]
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
import time
from pathlib import Path

# Ensure project root is on sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
os.environ.setdefault("CHROMA_TELEMETRY_ENABLED", "false")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
)
logger = logging.getLogger("reembed")


def parse_args():
    p = argparse.ArgumentParser(description="Re-embed Chroma DB with Ollama embeddings")
    p.add_argument(
        "--batch-size", type=int, default=50,
        help="Number of chunks per insert batch (default: 50)",
    )
    p.add_argument(
        "--model", default=None,
        help="Ollama embedding model (default: from OLLAMA_EMBED_MODEL env or nomic-embed-text)",
    )
    p.add_argument(
        "--no-backup", action="store_true",
        help="Skip creating a backup of the Chroma directory",
    )
    return p.parse_args()


def main():
    args = parse_args()

    from config import persist_directory

    chroma_dir = Path(persist_directory)
    if not chroma_dir.exists():
        logger.error("Chroma directory not found: %s", chroma_dir)
        sys.exit(1)

    # Optionally override the model via CLI
    if args.model:
        os.environ["OLLAMA_EMBED_MODEL"] = args.model

    # ── 0. Verify Ollama is running with the embedding model ─────────
    import json
    import urllib.request

    from core.chroma_factory import OLLAMA_EMBED_MODEL
    try:
        host = os.getenv("OLLAMA_HOST", "http://localhost:11434")
        resp = urllib.request.urlopen(f"{host}/api/tags", timeout=5)
        data = json.loads(resp.read())
        available = [m["name"] for m in data.get("models", [])]
        match = any(
            n == OLLAMA_EMBED_MODEL or n.startswith(f"{OLLAMA_EMBED_MODEL}:")
            for n in available
        )
        if not match:
            logger.error(
                "Ollama model '%s' not found. Available: %s\n"
                "Run:  ollama pull %s",
                OLLAMA_EMBED_MODEL, available, OLLAMA_EMBED_MODEL,
            )
            sys.exit(1)
        logger.info("Ollama OK — model '%s' available", OLLAMA_EMBED_MODEL)
    except Exception as exc:
        logger.error("Cannot reach Ollama at %s: %s", host, exc)
        sys.exit(1)

    # ── 1. Read all chunks from existing DB ──────────────────────────
    logger.info("Reading existing Chroma DB at %s …", chroma_dir)
    import chromadb

    client = chromadb.PersistentClient(path=str(chroma_dir))
    collections = client.list_collections()

    if not collections:
        logger.error("No collections found in Chroma DB")
        sys.exit(1)

    # Chroma's default LangChain collection name is "langchain"
    col = None
    for c in collections:
        name = c.name if hasattr(c, "name") else c
        if name == "langchain":
            col = client.get_collection("langchain")
            break
    if col is None:
        name0 = collections[0].name if hasattr(collections[0], "name") else collections[0]
        logger.warning("No 'langchain' collection — using '%s'", name0)
        col = client.get_collection(name0)

    # Fetch everything
    data = col.get(include=["documents", "metadatas"])
    all_ids = data["ids"]
    all_docs = data["documents"]
    all_metas = data["metadatas"]
    total = len(all_ids)
    logger.info("Read %d chunks from collection '%s'", total, col.name)

    if total == 0:
        logger.warning("Nothing to re-embed — collection is empty")
        sys.exit(0)

    # ── 2. Backup ────────────────────────────────────────────────────
    if not args.no_backup:
        backup = chroma_dir.parent / f"{chroma_dir.name}_backup_openai"
        if backup.exists():
            logger.info("Backup already exists at %s — skipping backup step", backup)
        else:
            logger.info("Backing up %s → %s …", chroma_dir, backup)
            shutil.copytree(chroma_dir, backup)
            logger.info("Backup complete")

    # ── 3. Delete old collection ─────────────────────────────────────
    logger.info("Deleting old collection '%s' …", col.name)
    col_name = col.name
    client.delete_collection(col_name)
    # Close out the old client completely
    del client

    # ── 4. Re-create with Ollama embeddings ──────────────────────────
    from core.chroma_factory import get_embeddings
    from langchain_chroma import Chroma

    embeddings = get_embeddings()

    vs = Chroma(
        collection_name=col_name,
        persist_directory=str(chroma_dir),
        embedding_function=embeddings,
    )

    # ── 5. Insert in batches ─────────────────────────────────────────
    batch = args.batch_size
    start = time.time()
    done = 0

    for i in range(0, total, batch):
        chunk_ids = all_ids[i : i + batch]
        chunk_docs = all_docs[i : i + batch]
        chunk_metas = all_metas[i : i + batch]

        # LangChain Chroma.add_texts accepts texts + metadatas + ids
        vs.add_texts(
            texts=chunk_docs,
            metadatas=chunk_metas,
            ids=chunk_ids,
        )
        done += len(chunk_ids)
        elapsed = time.time() - start
        rate = done / elapsed if elapsed > 0 else 0
        eta = (total - done) / rate if rate > 0 else 0
        logger.info(
            "  %d / %d  (%.1f chunks/s, ETA %.0fs)",
            done, total, rate, eta,
        )

    elapsed = time.time() - start
    logger.info(
        "Re-embedding complete: %d chunks in %.1fs (%.1f chunks/s)",
        total, elapsed, total / elapsed if elapsed > 0 else 0,
    )
    logger.info("New embeddings: %s via Ollama", OLLAMA_EMBED_MODEL)


if __name__ == "__main__":
    main()
