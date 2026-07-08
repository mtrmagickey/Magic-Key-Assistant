"""
Cleanup script to defer meta-documentation knowledge gaps that create infinite loops.
Run this once to clean up existing problematic gaps.
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "assistant.db"

META_DOC_PATTERNS = [
    "who is the primary owner",
    "who maintains",
    "who is responsible for maintaining",
    "when was the documentation last updated",
    "when was it last updated",
    "when was the last update",
    "what is the specific location of the official documentation",
    "what is the specific file path",
    "where is the official documentation",
    "where can i find the official documentation",
    "what is the process for updating",
    "what is the process for reviewing",
    "are there any specific constraints or requirements for maintaining",
    "who else is involved in the documentation process",
    "what is the source of truth for this documentation",
    "what are the specific responsibilities",
    "can you provide the name and role of the person responsible",
]

def main():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    # Check tables
    c.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = [r[0] for r in c.fetchall()]
    print(f"Tables: {tables}")
    
    if "knowledge_gaps" not in tables:
        print("knowledge_gaps table not found!")
        return
    
    # Find meta-doc gaps
    c.execute("SELECT id, topic, question, status, curation_status FROM knowledge_gaps WHERE status = 'open'")
    rows = c.fetchall()
    
    meta_gaps = []
    for row in rows:
        gap_id, topic, question, status, curation = row
        q_lower = (question or "").lower()
        t_lower = (topic or "").lower()
        
        # Check if it's a meta-documentation gap
        is_meta = False
        matched_pattern = None
        
        for pattern in META_DOC_PATTERNS:
            if pattern in q_lower:
                is_meta = True
                matched_pattern = pattern
                break
        
        # Also check if it's an "Open Question" about docs
        if "open question:" in t_lower and any(kw in q_lower for kw in ("documentation", "file path", "primary owner", "last updated", "who maintains")):
            is_meta = True
            matched_pattern = "Open Question about docs"
        
        if is_meta:
            meta_gaps.append((gap_id, topic[:50], question[:60], matched_pattern))
    
    print(f"\nFound {len(meta_gaps)} meta-documentation gaps to defer:\n")
    for gap in meta_gaps[:20]:
        print(f"  #{gap[0]}: {gap[1]}... | {gap[2]}... [{gap[3]}]")
    
    if len(meta_gaps) > 20:
        print(f"  ... and {len(meta_gaps) - 20} more")
    
    if not meta_gaps:
        print("No meta-documentation gaps found. Nothing to do.")
        return
    
    # Ask for confirmation
    confirm = input(f"\nDefer these {len(meta_gaps)} gaps? (y/n): ").strip().lower()
    if confirm != 'y':
        print("Aborted.")
        return
    
    # Update gaps to defer status
    for gap_id, _, _, pattern in meta_gaps:
        c.execute(
            "UPDATE knowledge_gaps SET curation_status = ?, curation_reason = ? WHERE id = ?",
            ("defer", f"auto:meta-documentation loop ({pattern[:40]})", gap_id)
        )
    
    conn.commit()
    print(f"\n✅ Deferred {len(meta_gaps)} meta-documentation gaps.")
    conn.close()

if __name__ == "__main__":
    main()
