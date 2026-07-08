import logging
import os
import random
import sys
from pathlib import Path

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("audit")

# Setup paths (assuming we run from project root)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(PROJECT_ROOT))
try:
    from config import persist_directory
except ImportError:
    print("Could not import config. Make sure to run inside the LeisureLLM environment.")
    sys.exit(1)

def audit_chroma_db():
    print(f"\n=== CHROMA DB AUDIT: {persist_directory} ===\n")
    
    # 1. Connect to Chroma
    if not os.path.exists(persist_directory):
        print(f"ERROR: Chroma directory not found at {persist_directory}")
        return

    from core.chroma_factory import get_vectorstore
    vectorstore = get_vectorstore()
    
    # 2. Check Collection Size (using get() which returns dict of ids, embeddings, docs, metadatas)
    try:
        data = vectorstore.get()
        ids = data['ids']
        metadatas = data['metadatas']
        documents = data['documents']
        count = len(ids)
        print(f"Total Chunks in DB: {count}")
    except Exception as e:
        print(f"Failed to fetch collection data: {e}")
        return

    if count == 0:
        print("WARNING: Database is empty!")
        return

    # 3. Analyze Metadata Distribution
    print("\n--- Metadata Distribution ---")
    sources = {}
    doc_types = {}
    
    for meta in metadatas:
        if not meta:
            continue
        
        # Source file frequency
        src = meta.get('source_relpath', 'unknown')
        sources[src] = sources.get(src, 0) + 1
        
        # Doc type frequency
        dtype = meta.get('doc_type', 'unknown')
        doc_types[dtype] = doc_types.get(dtype, 0) + 1

    print("\nTop 10 Source Files (by chunk count):")
    for src, cnt in sorted(sources.items(), key=lambda x: x[1], reverse=True)[:10]:
        print(f"  {cnt:4d} | {src}")

    print("\nDocument Types:")
    for dtype, cnt in sorted(doc_types.items(), key=lambda x: x[1], reverse=True):
        print(f"  {dtype}: {cnt}")

    # 4. Content Quality Check (Sample chunks)
    print("\n--- Content Sampling ---")
    indices = random.sample(range(count), min(3, count))
    for idx in indices:
        print(f"\n[Sample Chunk ID: {ids[idx]}]")
        print(f"Source: {metadatas[idx].get('source_relpath', 'unknown')}")
        content_preview = documents[idx][:300].replace('\n', ' ')
        print(f"Content: {content_preview}...")

    # 5. Retrieval Test
    print("\n--- Retrieval Efficacy Test ---")
    test_queries = [
        "project timeline and deliverables",
        "team member skills and expertise",
        "technical architecture overview"
    ]
    
    for q in test_queries:
        print(f"\nQuery: '{q}'")
        results = vectorstore.similarity_search_with_score(q, k=3)
        if not results:
            print("  NO RESULTS FOUND")
            continue
            
        for doc, score in results:
            # Note: Chroma score is L2 distance (lower is better) or cosine distance depending on config
            # Typically for OpenAI embeddings + Chroma defaults, it's L2 or Cosine distance.
            # If using Cosine Similarity, higher is better.
            # Let's just print the raw score
            src = doc.metadata.get('source_relpath', 'unknown')
            print(f"  Score: {score:.4f} | Source: {src}")
            print(f"  Snippet: {doc.page_content[:100]}...")

if __name__ == "__main__":
    audit_chroma_db()
