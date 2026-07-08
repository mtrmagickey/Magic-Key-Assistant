"""
SQLite Migration Runner
Simpler than PostgreSQL - just executes SQL files against SQLite database
"""
import asyncio
import sys
from pathlib import Path

import aiosqlite


async def run_migration(db_path: str, migration_file: str):
    """Run a SQL migration file"""
    migration_path = Path(migration_file)
    
    if not migration_path.exists():
        print(f"❌ Migration file not found: {migration_path}")
        sys.exit(1)
    
    print(f"📄 Reading migration: {migration_path.name}")
    sql = migration_path.read_text(encoding='utf-8')
    
    print(f"🔌 Connecting to SQLite database: {db_path}")
    
    try:
        async with aiosqlite.connect(db_path) as db:
            # Enable foreign keys
            await db.execute("PRAGMA foreign_keys = ON")
            
            # Execute migration
            print("⚙️  Executing SQL...")
            await db.executescript(sql)
            await db.commit()
            
            print("✅ Migration complete!\n")
            
            # Verify tables
            async with db.execute("""
                SELECT name FROM sqlite_master 
                WHERE type='table' AND name NOT LIKE 'sqlite_%'
                ORDER BY name
            """) as cursor:
                tables = await cursor.fetchall()
                
                if tables:
                    print("📊 Tables in database:")
                    for table in tables:
                        async with db.execute(f"SELECT COUNT(*) FROM {table[0]}") as count_cursor:
                            count = await count_cursor.fetchone()
                            print(f"   ✓ {table[0]} ({count[0]} rows)")
            
            print("\n🎉 Migration successful!")
            
    except Exception as e:
        print(f"❌ Error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python run_migration_sqlite.py <database_path> <migration_file>")
        print("Example: python run_migration_sqlite.py assistant.db migrations/001_initial_schema.sqlite.sql")
        sys.exit(1)
    
    db_path = sys.argv[1]
    migration_file = sys.argv[2]
    
    asyncio.run(run_migration(db_path, migration_file))
