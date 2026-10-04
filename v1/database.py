import os
from datetime import datetime, timezone
from typing import List, Optional
from sqlalchemy import (
    Column,
    Integer,
    String,
    Text,
    DateTime,
    ForeignKey,
    Table,
    text,
    func
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
    sessionmaker
)
from sqlalchemy.ext.asyncio import (
    create_async_engine,
    AsyncSession,
    async_sessionmaker
)
from sqlalchemy import create_engine
from loguru import logger

# Database Connection URLs from environment
DATABASE_URL = os.getenv(
    "DATABASE_URL", 
    "postgresql+asyncpg://postgres:postgres@db:5432/pauf_db"
)
SYNC_DATABASE_URL = os.getenv(
    "SYNC_DATABASE_URL", 
    "postgresql+psycopg2://postgres:postgres@db:5432/pauf_db"
)

# Pool Tuning Parameters
POOL_SIZE = int(os.getenv("DB_POOL_SIZE", "20"))
MAX_OVERFLOW = int(os.getenv("DB_MAX_OVERFLOW", "10"))
POOL_TIMEOUT = float(os.getenv("DB_POOL_TIMEOUT", "10.0"))
POOL_RECYCLE = int(os.getenv("DB_POOL_RECYCLE", "1800"))

# 1. Asynchronous Engine & SessionMaker (Primary Production Engine)
async_engine = create_async_engine(
    DATABASE_URL,
    pool_size=POOL_SIZE,
    max_overflow=MAX_OVERFLOW,
    pool_timeout=POOL_TIMEOUT,
    pool_pre_ping=True,       # Liveness test on checkout (SELECT 1)
    pool_recycle=POOL_RECYCLE,
    echo=False
)

AsyncSessionLocal = async_sessionmaker(
    bind=async_engine,
    class_=AsyncSession,
    expire_on_commit=False,   # Critical for asyncpg: prevents lazy reload crashes
    autoflush=False
)

# 2. Synchronous Engine & SessionMaker (Exclusively used in Phase 1 to demonstrate event loop blocking)
sync_engine = create_engine(
    SYNC_DATABASE_URL,
    pool_size=POOL_SIZE,
    max_overflow=MAX_OVERFLOW,
    pool_timeout=POOL_TIMEOUT,
    pool_pre_ping=True,
    echo=False
)
SyncSessionLocal = sessionmaker(
    bind=sync_engine,
    autocommit=False,
    autoflush=False
)

# 3. Declarative Base
class Base(DeclarativeBase):
    pass

# 4. Association Table for Many-to-Many: Notes <-> Tags
# Used in Phase 3 (N+1 Cascade) and Phase 4 (Pydantic Serialization)
note_tags = Table(
    "note_tags",
    Base.metadata,
    Column("note_id", Integer, ForeignKey("notes.id", ondelete="CASCADE"), primary_key=True),
    Column("tag_id", Integer, ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True)
)

# 5. Core Unified Models
class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(50), unique=True, index=True, nullable=False)
    email: Mapped[str] = mapped_column(String(100), nullable=False)
    
    # Counter column: Essential for Phase 7 (Lost Updates vs Atomic SQL vs Pessimistic Locks)
    note_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), 
        server_default=func.now(), 
        nullable=False
    )

    # Relationships
    notes: Mapped[List["Note"]] = relationship(
        "Note", 
        back_populates="user", 
        cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<User id={self.id} username='{self.username}'>"

class Note(Base):
    __tablename__ = "notes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, 
        ForeignKey("users.id", ondelete="CASCADE"), 
        nullable=False, 
        index=True
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    
    # Status column: Essential for Phase 6 (Composite Index on user_id, status)
    status: Mapped[str] = mapped_column(String(50), default="active", nullable=False)
    
    # Timestamp column: Essential for Phase 8 (Keyset / Cursor Pagination)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), 
        server_default=func.now(), 
        index=True, 
        nullable=False
    )

    # Relationships
    user: Mapped["User"] = relationship("User", back_populates="notes")
    tags: Mapped[List["Tag"]] = relationship(
        "Tag", 
        secondary=note_tags, 
        back_populates="notes"
    )

    def __repr__(self) -> str:
        return f"<Note id={self.id} user_id={self.user_id} title='{self.title}'>"

class Tag(Base):
    __tablename__ = "tags"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(50), unique=True, index=True, nullable=False)

    # Relationships
    notes: Mapped[List["Note"]] = relationship(
        "Note", 
        secondary=note_tags, 
        back_populates="tags"
    )

    def __repr__(self) -> str:
        return f"<Tag id={self.id} name='{self.name}'>"

# 6. Database Lifespan Functions
async def init_db() -> None:
    """
    Initialize database extensions and create all tables asynchronously.
    """
    logger.info("Initializing database schema and telemetry extensions...")
    async with async_engine.begin() as conn:
        # Enable pg_stat_statements extension inside PostgreSQL
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_stat_statements;"))
        # Create all declared tables if not existing
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database schema initialized successfully.")

async def close_db() -> None:
    """
    Cleanly dispose of database connection pools on application shutdown.
    """
    logger.info("Closing database engine connection pools...")
    await async_engine.dispose()
    sync_engine.dispose()
    logger.info("Database engine connection pools closed.")

if __name__ == "__main__":
    import asyncio
    async def test():
        await init_db()
        await close_db()
        print("Database schema test successful!")
    asyncio.run(test())
