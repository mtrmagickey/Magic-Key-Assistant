
import argparse
import csv
import hashlib
import logging

logger = logging.getLogger(__name__)
import os
import re as _re
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Ensure we can import LeisureLLM/config.py regardless of current working directory.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# ---- Disable Chroma telemetry noise ----
os.environ.setdefault("CHROMA_TELEMETRY_ENABLED", "false")

# ---- Third-party imports ----
import pandas as pd
from langchain_chroma import Chroma
from langchain_community.document_loaders import (
    JSONLoader,
    PyPDFLoader,
    TextLoader,
    UnstructuredWordDocumentLoader,
)
from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter

# ---- Project config ----
try:
    from config import directory_path, gpt_key, hash_csv, persist_directory
except Exception as e:
    raise RuntimeError(
        "Could not import required settings from config.py. "
        "Make sure config.py is in the project root and that you are running this script "
        "from the project root (the folder that contains config.py)."
    ) from e

# This script is typically executed with CWD at LeisureLLM/ (see DocumentAuthor).
# Use that directory as a stable anchor for relative paths in logs and metadata.
ROOT_DIR = str(Path(__file__).resolve().parent.parent)


def _is_admin_contributed(path: str) -> bool:
    """Check if a Markdown file has ``source: admin_ui`` in its YAML frontmatter.

    This distinguishes human-contributed knowledge (entered via the admin
    console) from bot-generated memos written by DocumentAuthor.
    """
    if not path.lower().endswith(".md"):
        return False
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            first_line = f.readline().strip()
            if first_line != "---":
                return False
            for _ in range(20):
                line = f.readline()
                if not line:
                    break
                stripped = line.strip()
                if stripped == "---":
                    break
                if stripped == "source: admin_ui":
                    return True
    except Exception as e:
        logger.warning("_is_admin_contributed: suppressed %s", e)
    return False


def infer_metadata(path: str) -> Dict[str, object]:
    """Derive lightweight metadata used to steer retrieval quality.

    We intentionally keep this simple and deterministic:
    - Prefer raw/primary sources (discord exports, docs) over generated memos.
    - Treat human-contributed knowledge (admin UI, web inbox, knowledge
      folders) as primary sources alongside raw docs.
    - Provide a stable relative path for citations.
    - Extract date ranges from filenames and file modification times.
    """
    relpath = os.path.relpath(path, ROOT_DIR)
    norm = path.replace("\\", "/").lower()

    # Defaults
    doc_type = "doc"
    source_kind = "primary"
    source_priority = 0

    # Heuristics by location — order matters (specific before general)
    if any(seg in norm for seg in ("/docs/admin_answers/", "/docs/web_inbox/", "/docs/knowledge/", "/docs/onboarding/")):
        # Human-contributed content via admin UI or web chat
        doc_type = "human_knowledge"
        source_kind = "primary"
        source_priority = 2
    elif "/docs/memos/" in norm:
        # Memos: could be bot-generated OR human-contributed via admin UI
        if _is_admin_contributed(path):
            doc_type = "human_knowledge"
            source_kind = "primary"
            source_priority = 2
        else:
            doc_type = "memo"
            source_kind = "generated"
            source_priority = -2
    elif "/docs/interview/" in norm:
        doc_type = "interview_memo"
        source_kind = "generated"
        source_priority = -1
    elif norm.endswith("_lines.txt") or norm.endswith("_lines.md"):
        doc_type = "discord_export"
        source_kind = "primary"
        source_priority = 2
    elif "/docs/" in norm:
        doc_type = "doc"
        source_kind = "primary"
        source_priority = 1

    # --- Date extraction from filename / path ---
    doc_date = ""
    # Try patterns like "2025", "2026-01-15", "May 2025", etc. in the filename
    basename = os.path.basename(path)
    iso_match = _re.search(r'(20\d{2})[-_](\d{1,2})[-_](\d{1,2})', basename)
    year_match = _re.search(r'(20\d{2})', basename)
    if iso_match:
        doc_date = f"{iso_match.group(1)}-{iso_match.group(2).zfill(2)}-{iso_match.group(3).zfill(2)}"
    elif year_match:
        doc_date = year_match.group(1)

    # Fall back to file modification time
    file_mtime = ""
    try:
        mtime = os.path.getmtime(path)
        file_mtime = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d")
    except Exception as e:
        logger.warning("operation: suppressed %s", e)

    result = {
        "source": path,
        "source_relpath": relpath,
        "doc_type": doc_type,
        "source_kind": source_kind,
        "source_priority": source_priority,
    }
    if doc_date:
        result["doc_date"] = doc_date
    if file_mtime:
        result["file_modified"] = file_mtime
    return result

# ---- Embedding + splitting ----
from core.chroma_factory import get_embeddings

embedding_function = get_embeddings()
text_splitter = RecursiveCharacterTextSplitter(chunk_size=2000, chunk_overlap=400)

# ---------------------------
# Helpers
# ---------------------------
def setup_cli() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Ingest docs into Chroma (incremental).")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose logging to console.")
    return parser.parse_args()

def setup_logging(verbose: bool):
    level = logging.INFO if verbose else logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=[logging.StreamHandler()]
    )

def file_hash_sha3_512(path: str) -> str:
    h = hashlib.sha3_512()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def safe_is_text_ext(path: str) -> bool:
    """Filter to allowed doc types."""
    ext = os.path.splitext(path)[1].lower()
    # Include .md because the bot writes memos/notes as Markdown.
    return ext in {".pdf", ".docx", ".json", ".jsonl", ".txt", ".md"}

def list_all_files(root: str) -> List[str]:
    files = []
    for p, _, fs in os.walk(root):
        for name in fs:
            full = os.path.join(p, name)
            if safe_is_text_ext(full):
                # Skip documents flagged as needs_review (auto-generated, not yet approved)
                if _is_needs_review(full):
                    logging.info(f"Skipping needs_review document: {full}")
                    continue
                files.append(full)
            else:
                logging.info(f"Skipping unsupported file type: {full}")
    return files


def _is_needs_review(path: str) -> bool:
    """Check if a Markdown file has 'status: needs_review' in its YAML frontmatter.

    This prevents auto-generated improvement memos from being ingested into
    the vector store until a human has reviewed and approved them.
    Only inspects .md files; other formats are never gated.
    """
    if not path.lower().endswith(".md"):
        return False
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            first_line = f.readline().strip()
            if first_line != "---":
                return False
            # Read up to 30 lines of frontmatter (enough for any realistic header)
            for _ in range(30):
                line = f.readline()
                if not line:
                    break
                stripped = line.strip()
                if stripped == "---":
                    break
                if stripped == "status: needs_review":
                    return True
    except Exception as e:
        logger.warning("_is_needs_review: suppressed %s", e)
    return False

def load_text_any_encoding(path: str) -> str:
    """
    Try UTF-8, then Windows-1252, then Latin-1 to avoid crashing on Windows smart quotes, etc.
    """
    encodings = ["utf-8", "cp1252", "latin-1"]
    for enc in encodings:
        try:
            with open(path, "r", encoding=enc, errors="strict") as f:
                return f.read()
        except UnicodeDecodeError:
            continue
    # Last resort: permissive read that replaces bad bytes
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()

def load_jsonl_as_docs(path: str) -> List[Document]:
    docs: List[Document] = []
    # Read line by line, each line is a JSON object (or plain text fallback)
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        full_content_buffer = []
        for idx, line in enumerate(f):
            line_stripped = line.strip()
            if not line_stripped:
                continue
            try:
                # Try parse JSON per line
                import json
                obj = json.loads(line_stripped)
                
                # SPECIAL HANDLING for Discord exports that wrapped messages in {"text": "..."}
                # Also handle LangChain exports that use {"page_content": "..."}
                if isinstance(obj, dict):
                    if "text" in obj:
                        content = obj["text"]
                    elif "page_content" in obj:
                        content = obj["page_content"]
                    else:
                        content = json.dumps(obj, ensure_ascii=False)
                else:
                    content = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)
            except Exception:
                content = line_stripped
            
            full_content_buffer.append(str(content))
        
        # Merge all lines into one document for better context, instead of 1 doc per line
        # The text splitter will handle breaking it up.
        merged_content = "\n".join(full_content_buffer)
        docs.append(Document(page_content=merged_content, metadata={"source": path}))
        
    return docs

# ---------------------------------------------------------------------------
# Content sanitisation — strip indirect prompt-injection markers
# ---------------------------------------------------------------------------

# Patterns that look like injected system/role instructions.
# We collapse them to a benign placeholder so the text remains readable
# but the LLM will not interpret it as a command.
_INJECTION_PATTERNS: list[tuple[_re.Pattern, str]] = [
    # Chat-ML / Llama / Mistral role tokens
    (_re.compile(r"<\|im_start\|>\s*(system|user|assistant)", _re.IGNORECASE), "[role-marker-removed]"),
    (_re.compile(r"<\|im_end\|>", _re.IGNORECASE), ""),
    (_re.compile(r"<\|system\|>", _re.IGNORECASE), "[role-marker-removed]"),
    (_re.compile(r"<\|user\|>", _re.IGNORECASE), "[role-marker-removed]"),
    (_re.compile(r"<\|assistant\|>", _re.IGNORECASE), "[role-marker-removed]"),
    # Explicit instruction-override phrases
    (_re.compile(
        r"(IGNORE|FORGET|DISREGARD|OVERRIDE)\s+(ALL\s+)?(PREVIOUS|PRIOR|ABOVE|EARLIER|PRECEDING)\s+(INSTRUCTIONS|CONTEXT|RULES|DIRECTIVES)",
        _re.IGNORECASE,
    ), "[instruction-override-removed]"),
    # Fake role preambles ("SYSTEM:", "### SYSTEM:", "[SYSTEM]")
    (_re.compile(r"^\s*(?:###\s*)?(?:SYSTEM|ASSISTANT|ADMIN)\s*:", _re.IGNORECASE | _re.MULTILINE), "[role-preamble-removed]:"),
    (_re.compile(r"\[(?:SYSTEM|ADMIN)\]", _re.IGNORECASE), "[role-preamble-removed]"),
    # "You are now ...", "Act as ...", "Pretend to be ..." at line start
    (_re.compile(
        r"^\s*(You\s+are\s+now|From\s+now\s+on\s+you\s+are|Act\s+as|Pretend\s+to\s+be|Roleplay\s+as)\b",
        _re.IGNORECASE | _re.MULTILINE,
    ), "[role-override-removed]"),
    # Prompt-leak requests
    (_re.compile(
        r"(output|reveal|repeat|print|show)\s+(your|the)\s+(system\s+prompt|instructions|rules)",
        _re.IGNORECASE,
    ), "[prompt-leak-removed]"),
]


def sanitize_chunk_content(text: str) -> str:
    """Strip known prompt-injection markers from document text.

    This is a defence-in-depth measure for the RAG pipeline: even though
    the system prompt tells the LLM to treat retrieved context as *data*,
    we proactively remove the most common injection payloads so they never
    reach the model context window.

    The function is intentionally conservative — it only removes patterns
    that have no plausible reason to appear in legitimate business documents.
    """
    for pattern, replacement in _INJECTION_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def chunk_documents(docs: List[Document]) -> List[Document]:
    chunks = text_splitter.split_documents(docs)
    for chunk in chunks:
        chunk.page_content = sanitize_chunk_content(chunk.page_content)
    return chunks

def load_file(path: str) -> List[Document]:
    # ── File validation gate ──
    _MAX_FILE_SIZE_MB = 50
    file_size = os.path.getsize(path)
    if file_size > _MAX_FILE_SIZE_MB * 1024 * 1024:
        raise ValueError(f"File too large ({file_size / 1024 / 1024:.1f} MB > {_MAX_FILE_SIZE_MB} MB limit): {path}")

    ext = os.path.splitext(path)[1].lower()
    _ALLOWED_EXTENSIONS = {".pdf", ".docx", ".json", ".jsonl", ".txt", ".md"}
    if ext not in _ALLOWED_EXTENSIONS and not path.endswith("_lines.txt"):
        raise ValueError(f"Unsupported file type '{ext}'. Allowed: {', '.join(sorted(_ALLOWED_EXTENSIONS))}")
    
    # Handle specific filename patterns that behave like JSONL
    if path.endswith("_lines.txt") or ext == ".jsonl":
        return load_jsonl_as_docs(path)

    if ext == ".pdf":
        return PyPDFLoader(path).load()
    elif ext == ".docx":
        return UnstructuredWordDocumentLoader(path).load()
    elif ext == ".json":
        # Load array or object entries; fallback is entire file if not an array
        try:
            return JSONLoader(file_path=path, jq_schema=".[]", text_content=False).load()
        except Exception:
            try:
                return JSONLoader(file_path=path, jq_schema=".", text_content=False).load()
            except Exception:
                # Final fallback: raw text
                txt = load_text_any_encoding(path)
                return [Document(page_content=txt, metadata={"source": path})]
    elif ext == ".jsonl":
        return load_jsonl_as_docs(path)
    elif ext in {".txt", ".md"}:
        txt = load_text_any_encoding(path)
        return [Document(page_content=txt, metadata={"source": path})]
    else:
        raise ValueError(f"Unsupported file extension: {ext}")

def read_hash_table(csv_path: str) -> pd.DataFrame:
    if not os.path.isfile(csv_path):
        return pd.DataFrame(columns=["FileName", "Hash"])
    try:
        return pd.read_csv(csv_path)
    except Exception:
        # If CSV corrupt, start fresh
        return pd.DataFrame(columns=["FileName", "Hash"])

def write_hash_table(csv_path: str, rows: List[Tuple[str, str]]):
    df = pd.DataFrame(rows, columns=["FileName", "Hash"])
    df.to_csv(csv_path, index=False)

def summarize_db(db: Chroma) -> int:
    try:
        raw = db.get()
        return len(raw.get("ids", []))
    except Exception:
        return -1

# ---------------------------
# Core ingest
# ---------------------------
def run_ingest() -> Tuple[Chroma, Dict[str, int], str]:
    """
    Returns: (db, stats, persist_directory)
    """
    logging.info("Incremental ingest: comparing hashes.")

    # Prepare Chroma
    try:
        from core.chroma_factory import get_vectorstore
        db = get_vectorstore(
            persist_directory=persist_directory,
            embedding_function=embedding_function,
        )
    except Exception:
        db = Chroma(persist_directory=persist_directory, embedding_function=embedding_function)

    # Determine changes
    prev = read_hash_table(hash_csv)
    prev_map = dict(zip(prev.get("FileName", []), prev.get("Hash", [])))

    all_files = list_all_files(directory_path)
    to_add_or_update: List[str] = []
    to_delete: List[str] = []
    new_hash_rows: List[Tuple[str, str]] = []

    for f in all_files:
        h = file_hash_sha3_512(f)
        prev_h = prev_map.get(f)
        rel = os.path.relpath(f, ROOT_DIR)
        if prev_h is None:
            logging.info(f"{rel}: new.")
            to_add_or_update.append(f)
        elif prev_h != h:
            logging.info(f"{rel}: changed.")
            to_add_or_update.append(f)
        else:
            logging.info(f"{rel}: unchanged.")
        new_hash_rows.append((f, h))

    # Identify deletions (present in old, missing now)
    for old_file in prev_map:
        if not os.path.isfile(old_file):
            rel = os.path.relpath(old_file, ROOT_DIR) if os.path.isabs(old_file) else old_file
            logging.info(f"{rel}: missing now -> delete from index.")
            to_delete.append(old_file)

    # Load existing docs metadata from Chroma to get IDs for deletion
    raw = db.get()
    existing_ids: List[str] = raw.get("ids", [])
    existing_metas: List[dict] = raw.get("metadatas", [])

    id_by_source: Dict[str, List[str]] = {}
    for _id, meta in zip(existing_ids, existing_metas):
        src = (meta or {}).get("source")
        if src:
            id_by_source.setdefault(src, []).append(_id)

    # Stats
    stats = {
        "added_files": 0,
        "updated_files": 0,
        "unchanged_files": len(all_files) - len(to_add_or_update),
        "skipped_files": 0,
        "chunks_written": 0,
        "deleted_files": 0,
    }

    # Deletions first
    for f in to_delete:
        ids = id_by_source.get(f, [])
        if ids:
            try:
                db.delete(ids)
                stats["deleted_files"] += 1
            except Exception as e:
                logging.warning(f"Failed to delete {len(ids)} chunks for {f}: {e}")

    # Add/update files
    for f in to_add_or_update:
        try:
            docs = load_file(f)
            # Ensure source metadata always present
            for d in docs:
                d.metadata = d.metadata or {}
                d.metadata.update(infer_metadata(f))

            chunks = chunk_documents(docs)
            # Ensure chunk metadata includes our derived fields (splitters should preserve,
            # but make it explicit in case of future changes).
            derived = infer_metadata(f)
            for c in chunks:
                c.metadata = c.metadata or {}
                for k, v in derived.items():
                    c.metadata.setdefault(k, v)
            if not chunks:
                logging.info(f"No chunks produced (empty?) -> skipped: {f}")
                stats["skipped_files"] += 1
                continue

            # If exists, delete old chunks for this source to avoid duplicates
            old_ids = id_by_source.get(f, [])
            if old_ids:
                try:
                    db.delete(old_ids)
                except Exception as e:
                    logging.warning(f"Could not delete old chunks for {f}: {e}")

            # Generate deterministic IDs for chunks of this file
            ids = [f"{f}_{i}" for i in range(len(chunks))]

            # Mark chunks as not-yet-enriched so the background enricher picks them up
            for c in chunks:
                c.metadata.setdefault("enriched", False)

            # Add to Chroma
            db.add_documents(chunks, ids=ids)
            stats["chunks_written"] += len(chunks)

            # Count as add vs update
            if f in prev_map:
                stats["updated_files"] += 1
            else:
                stats["added_files"] += 1

        except Exception as e:
            logging.warning(f"Failed to process {f}: {e}")
            stats["skipped_files"] += 1

    # Persist new hash table
    write_hash_table(hash_csv, new_hash_rows)

    return db, stats, persist_directory

def print_summary(stats: Dict[str, int], db: Chroma, persist_dir: str):
    total_vecs = summarize_db(db)
    print("\n=== Ingest Summary ===")
    print(f"Persist dir:        {persist_dir}")
    print(f"Added files:        {stats['added_files']}")
    print(f"Updated files:      {stats['updated_files']}")
    print(f"Deleted files:      {stats['deleted_files']}")
    print(f"Unchanged files:    {stats['unchanged_files']}")
    print(f"Skipped files:      {stats['skipped_files']}")
    print(f"Chunks written:     {stats['chunks_written']}")
    if total_vecs >= 0:
        print(f"Total vectors now:  {total_vecs}")
    else:
        print("Total vectors now:  (unavailable)")
    if all(stats[k] == 0 for k in ("added_files", "updated_files", "deleted_files", "chunks_written")):
        print("No changes detected. Index is already up to date.")
    print("======================\n")

# ---------------------------
# Main
# ---------------------------
if __name__ == "__main__":
    args = setup_cli()
    setup_logging(args.verbose)

    try:
        db, stats, persist_dir = run_ingest()
        print_summary(stats, db, persist_dir)
    except Exception as e:
        logging.exception(f"Ingest failed: {e}")
        print("\nIngest failed. See traceback above for details.\n")
        sys.exit(1)

# --- keep at very end of cogs/ingest_metadata.py ---
async def setup(bot):
    # This module is a standalone ingest script, not a Cog.
    # Loading it as an extension is a no-op to satisfy the loader.
    logging.getLogger(__name__).info("ingest_metadata: loaded as no-op extension.")

