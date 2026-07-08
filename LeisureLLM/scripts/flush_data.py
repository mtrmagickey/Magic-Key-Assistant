import argparse
import asyncio
import sys
from pathlib import Path

# Add parent directory to path to import database
sys.path.append(str(Path(__file__).parent))

from database import Database


async def flush_data(all_tables: bool = False, skip_confirm: bool = False):
    db_path = "LeisureLLM/assistant.db"
    
    if not Path(db_path).exists():
        print(f"Database not found at {db_path}")
        return

    db = Database(db_path)
    await db.connect()

    # Tables that represent "working memory" and likely contain the anchoring bias
    tables_to_flush = [
        "knowledge_gaps",
        "tasks",
        "action_gap_links",
        "meeting_agenda_items",
        "escalations",
        "job_runs" # Reset job history so it re-runs fresh
    ]

    # Tables that represent "structural history" or stats (optional to flush)
    if all_tables:
        tables_to_flush.extend([
            "sprint_cycles",
            "partner_engagement",
            "partner_point_events"
        ])

    print(f"You are about to DELETE ALL DATA from these tables in {db_path}:")
    for t in tables_to_flush:
        print(f"  - {t}")
    
    if not skip_confirm:
        confirm = input("\nType 'yes' to proceed with deletion: ")
        if confirm.lower() != "yes":
            print("Aborted.")
            await db.close()
            return

    async with db.acquire() as conn:
        for table in tables_to_flush:
            try:
                # Check if table exists first
                cursor = await conn.execute(f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table}'")
                if await cursor.fetchone():
                    await conn.execute(f"DELETE FROM {table}")
                    print(f"  [✓] Flushed {table}")
                else:
                    print(f"  [-] Table {table} not found, skipping.")
            except Exception as e:
                print(f"  [!] Error flushing {table}: {e}")
        
        await conn.execute("VACUUM")
        print("  [✓] Database vacuumed")
        await conn.commit()
    
    await db.close()
    print("\nFlush complete. Please restart the bot to ensure no in-memory caches remain.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Flush working memory from LeisureLLM database.")
    parser.add_argument("--all", action="store_true", help="Flush ALL tables including stats and sprints")
    parser.add_argument("--yes", action="store_true", help="Skip confirmation prompt")
    args = parser.parse_args()

    # Skip confirmation if --yes is passed
    if not args.yes:
        confirm = input("\nType 'yes' to proceed with deletion: ")
        if confirm.lower() != "yes":
            print("Aborted.")
            sys.exit(0)

    asyncio.run(flush_data(all_tables=args.all, skip_confirm=True))
