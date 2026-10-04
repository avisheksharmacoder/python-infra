import asyncio
import time
import random
from datetime import datetime, timezone, timedelta
from sqlalchemy import select, func, insert, delete
from loguru import logger

try:
    from .database import async_engine, User, Note, Tag, note_tags, init_db
except ImportError:
    from database import async_engine, User, Note, Tag, note_tags, init_db

# Seeding Constants
TOTAL_USERS = 1000
NOTES_PER_USER = 100
TOTAL_NOTES = TOTAL_USERS * NOTES_PER_USER  # 100,000 notes
BATCH_SIZE = 5000

TAG_NAMES = [
    "work", "personal", "urgent", "project", "ideas",
    "dev", "db", "fastapi", "postgres", "infra",
    "study", "todo", "meeting", "notes", "bug",
    "feature", "release", "archive", "system", "security"
]

async def seed_database(reset: bool = False):
    """
    Deterministically seeds PostgreSQL with:
      - 1,000 active users
      - 20 tags
      - 100,000 notes (100 per user, 70% active, 30% archived)
      - ~200,000 note_tags associations
    Uses high-speed batched multi-row inserts for maximum throughput.
    """
    start_time = time.perf_counter()
    logger.info("Connecting to database and verifying schema...")
    await init_db()

    async with async_engine.begin() as conn:
        # Check current counts
        user_count = (await conn.execute(select(func.count(User.id)))).scalar() or 0
        note_count = (await conn.execute(select(func.count(Note.id)))).scalar() or 0

        if reset and (user_count > 0 or note_count > 0):
            logger.warning("Reset flag detected. Purging existing tables...")
            await conn.execute(delete(note_tags))
            await conn.execute(delete(Note))
            await conn.execute(delete(Tag))
            await conn.execute(delete(User))
            user_count = 0
            note_count = 0

        if user_count >= TOTAL_USERS and note_count >= TOTAL_NOTES:
            logger.info(f"Database already seeded ({user_count} users, {note_count} notes). Skipping seed.")
            return

        logger.info(f"Beginning seed pipeline -> Target: {TOTAL_USERS:,} Users, {TOTAL_NOTES:,} Notes...")

        # 1. Seed Users (1,000 users)
        if user_count < TOTAL_USERS:
            logger.info(f"Seeding {TOTAL_USERS} Users...")
            now = datetime.now(timezone.utc)
            users_payload = [
                {
                    "username": f"user_{i}",
                    "email": f"user_{i}@example.com",
                    "note_count": NOTES_PER_USER,
                    "created_at": now - timedelta(days=random.randint(30, 365))
                }
                for i in range(1, TOTAL_USERS + 1)
            ]
            await conn.execute(insert(User), users_payload)
            logger.info(f"Successfully seeded {TOTAL_USERS} users.")

        # 2. Seed Tags (20 tags)
        tag_count = (await conn.execute(select(func.count(Tag.id)))).scalar() or 0
        if tag_count < len(TAG_NAMES):
            logger.info(f"Seeding {len(TAG_NAMES)} Tags...")
            tags_payload = [{"name": name} for name in TAG_NAMES]
            await conn.execute(insert(Tag), tags_payload)
            logger.info("Successfully seeded tags.")

        # Query user IDs and tag IDs
        user_ids = (await conn.execute(select(User.id))).scalars().all()
        tag_ids = (await conn.execute(select(Tag.id))).scalars().all()

        # 3. Seed Notes in high-performance batches (100,000 notes total)
        current_notes = (await conn.execute(select(func.count(Note.id)))).scalar() or 0
        if current_notes < TOTAL_NOTES:
            logger.info(f"Seeding {TOTAL_NOTES:,} Notes in batches of {BATCH_SIZE:,}...")
            notes_created = 0
            now = datetime.now(timezone.utc)

            note_records_batch = []
            for user_id in user_ids:
                for note_idx in range(1, NOTES_PER_USER + 1):
                    # 70% active, 30% archived
                    status = "active" if random.random() < 0.70 else "archived"
                    created_at = now - timedelta(
                        days=random.randint(1, 180),
                        hours=random.randint(0, 23),
                        minutes=random.randint(0, 59)
                    )
                    note_records_batch.append({
                        "user_id": user_id,
                        "title": f"Note {note_idx} for User {user_id}",
                        "content": f"High-performance content payload for note #{note_idx} belonging to user {user_id}. Benchmarking storage and indexing engines.",
                        "status": status,
                        "created_at": created_at
                    })

                    if len(note_records_batch) >= BATCH_SIZE:
                        # Execute batched multi-row INSERT
                        result = await conn.execute(
                            insert(Note).returning(Note.id),
                            note_records_batch
                        )
                        inserted_ids = result.scalars().all()

                        # Link 2 random tags per note
                        note_tag_pairs = []
                        for n_id in inserted_ids:
                            chosen_tags = random.sample(tag_ids, min(2, len(tag_ids)))
                            for t_id in chosen_tags:
                                note_tag_pairs.append({"note_id": n_id, "tag_id": t_id})

                        if note_tag_pairs:
                            await conn.execute(insert(note_tags), note_tag_pairs)

                        notes_created += len(note_records_batch)
                        logger.info(f"Progress: {notes_created:,} / {TOTAL_NOTES:,} notes seeded...")
                        note_records_batch = []

    elapsed = round(time.perf_counter() - start_time, 2)
    logger.info(f"Seeding pipeline complete! Total elapsed time: {elapsed}s.")

if __name__ == "__main__":
    import sys
    should_reset = "--reset" in sys.argv
    asyncio.run(seed_database(reset=should_reset))
