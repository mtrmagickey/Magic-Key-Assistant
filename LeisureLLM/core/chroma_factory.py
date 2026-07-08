"""
Chroma vector store factory.

Provides:
- ``get_embeddings()`` — returns the project-wide LangChain embedding
  function (Ollama ``nomic-embed-text`` preferred; falls back to OpenAI
  ``text-embedding-3-large`` if Ollama is unreachable).
- ``get_vectorstore()`` — returns a LangChain Chroma instance configured
  for either embedded (local dev) or HTTP (Docker) mode.

Usage:
    from core.chroma_factory import get_embeddings, get_vectorstore
    emb = get_embeddings()
    vs  = get_vectorstore(embedding_function=emb)
"""

from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# ── Embedding model constants ───────────────────────────────────────────────
OLLAMA_EMBED_MODEL = os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text")
OPENAI_EMBED_MODEL = "text-embedding-3-large"

# Cached singleton so every call-site shares one instance
_embedding_instance = None


def get_embeddings(*, force_openai: bool = False):
    """Return the project-wide embedding function.

    Resolution order:
    1. If ``force_openai`` is True → OpenAI (needs ``OPENAI_API_KEY``).
    2. Ollama embedding model (``nomic-embed-text`` by default) if the
       Ollama server is reachable at ``OLLAMA_HOST`` / ``localhost:11434``.
    3. OpenAI ``text-embedding-3-large`` as fallback.

    The result is cached as a module-level singleton.
    """
    global _embedding_instance
    if _embedding_instance is not None:
        return _embedding_instance

    if not force_openai:
        emb = _try_ollama_embeddings()
        if emb is not None:
            _embedding_instance = emb
            return _embedding_instance

    # Fallback to OpenAI
    emb = _get_openai_embeddings()
    if emb is not None:
        _embedding_instance = emb
        return _embedding_instance

    raise RuntimeError(
        "No embedding backend available. Either start Ollama with "
        f"'{OLLAMA_EMBED_MODEL}' pulled, or set OPENAI_API_KEY."
    )


def _try_ollama_embeddings():
    """Try to build OllamaEmbeddings; return None if Ollama is unreachable."""
    try:
        import urllib.request
        host = os.getenv("OLLAMA_HOST", "http://localhost:11434")
        url = f"{host}/api/tags"
        resp = urllib.request.urlopen(url, timeout=2)
        import json
        data = json.loads(resp.read())
        available = [m["name"] for m in data.get("models", [])]
        # Accept both "nomic-embed-text" and "nomic-embed-text:latest"
        match = any(
            n == OLLAMA_EMBED_MODEL or n.startswith(f"{OLLAMA_EMBED_MODEL}:")
            for n in available
        )
        if not match:
            logger.warning(
                "Ollama running but '%s' not pulled (available: %s). "
                "Falling back to OpenAI embeddings.",
                OLLAMA_EMBED_MODEL,
                available,
            )
            return None

        from langchain_ollama import OllamaEmbeddings
        base_url = os.getenv("OLLAMA_HOST", "http://localhost:11434")
        emb = OllamaEmbeddings(model=OLLAMA_EMBED_MODEL, base_url=base_url)
        logger.info("Using Ollama embeddings: model=%s", OLLAMA_EMBED_MODEL)
        return emb
    except Exception as exc:
        logger.debug("Ollama embeddings unavailable: %s", exc)
        return None


def _get_openai_embeddings():
    """Return OpenAIEmbeddings if an API key is configured."""
    try:
        from config import gpt_key
    except ImportError:
        gpt_key = os.getenv("OPENAI_API_KEY")

    if not gpt_key:
        return None

    from langchain_openai import OpenAIEmbeddings
    logger.info("Using OpenAI embeddings: model=%s", OPENAI_EMBED_MODEL)
    return OpenAIEmbeddings(model=OPENAI_EMBED_MODEL, api_key=gpt_key)


def reset_embeddings():
    """Clear the cached embedding instance (useful for tests or when
    switching backends at runtime)."""
    global _embedding_instance
    _embedding_instance = None


# ── Vectorstore factory ─────────────────────────────────────────────────────


def get_vectorstore(
    persist_directory: Optional[str] = None,
    embedding_function=None,
):
    """
    Return a LangChain Chroma vectorstore in the correct mode.

    - If CHROMA_HOST is set → HTTP client (Docker / remote Chroma server)
    - Otherwise → embedded PersistentClient using persist_directory

    If ``embedding_function`` is None, ``get_embeddings()`` is called
    automatically.
    """
    from langchain_chroma import Chroma

    if embedding_function is None:
        embedding_function = get_embeddings()

    chroma_host = os.getenv("CHROMA_HOST", "")

    if chroma_host:
        # Docker / remote: connect over HTTP
        import chromadb

        logger.info("Connecting to Chroma HTTP server at %s", chroma_host)
        http_client = chromadb.HttpClient(host=chroma_host)
        return Chroma(
            client=http_client,
            embedding_function=embedding_function,
        )
    else:
        # Local dev: embedded PersistentClient
        if not persist_directory:
            from config import persist_directory as default_dir
            persist_directory = default_dir

        logger.info("Using embedded Chroma at %s", persist_directory)
        return Chroma(
            persist_directory=persist_directory,
            embedding_function=embedding_function,
        )
