"""
OpenOwl Memory System
- Redis: Short-term (active session context, fast reads)
- PostgreSQL: Long-term (user preferences, task history, facts)
- pgvector: Semantic search over past conversations
"""
import json
import logging
from datetime import datetime, timedelta
from typing import Optional, Any
from uuid import uuid4

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)


class RedisMemory:
    """
    Short-term memory: stores active conversation context.
    Data expires automatically — no manual cleanup needed.
    """

    def __init__(self, redis_url: str):
        self.redis_url = redis_url
        self._client: Optional[aioredis.Redis] = None

    async def connect(self):
        self._client = await aioredis.from_url(
            self.redis_url,
            encoding="utf-8",
            decode_responses=True,
        )
        logger.info("✅ Redis connected")

    async def close(self):
        if self._client:
            await self._client.close()

    # ── Session context ──────────────────────────────────────────────────────

    async def get_session(self, user_id: str) -> dict:
        """Get active session data for a user."""
        key = f"session:{user_id}"
        data = await self._client.get(key)
        return json.loads(data) if data else {}

    async def set_session(self, user_id: str, data: dict, ttl_seconds: int = 3600):
        """Store session data with 1-hour TTL by default."""
        key = f"session:{user_id}"
        await self._client.setex(key, ttl_seconds, json.dumps(data))

    async def update_session(self, user_id: str, updates: dict):
        """Merge updates into existing session."""
        current = await self.get_session(user_id)
        current.update(updates)
        await self.set_session(user_id, current)

    # ── Conversation history (last N messages) ────────────────────────────────

    async def append_message(self, user_id: str, message: dict, max_messages: int = 10):
        """Add a message to the conversation buffer."""
        key = f"history:{user_id}"
        await self._client.rpush(key, json.dumps(message))
        # Keep only last N messages
        await self._client.ltrim(key, -max_messages, -1)
        await self._client.expire(key, 86400)  # 24h TTL

    async def get_history(self, user_id: str) -> list[dict]:
        """Get recent conversation history."""
        key = f"history:{user_id}"
        messages = await self._client.lrange(key, 0, -1)
        return [json.loads(m) for m in messages]

    async def clear_history(self, user_id: str):
        await self._client.delete(f"history:{user_id}")

    # ── Pending approvals (stored with TTL) ──────────────────────────────────

    async def store_pending_approval(
        self, task_id: str, approval_data: dict, ttl_seconds: int = 300
    ):
        """Store an approval request. Expires in 5 min by default."""
        key = f"approval:{task_id}"
        await self._client.setex(key, ttl_seconds, json.dumps(approval_data))

    async def get_pending_approval(self, task_id: str) -> Optional[dict]:
        key = f"approval:{task_id}"
        data = await self._client.get(key)
        return json.loads(data) if data else None

    async def resolve_approval(self, task_id: str, approved: bool, user_id: str):
        """Mark an approval as resolved."""
        key = f"approval:{task_id}"
        data = await self.get_pending_approval(task_id)
        if data:
            data["status"] = "approved" if approved else "denied"
            data["resolved_by"] = user_id
            data["resolved_at"] = datetime.utcnow().isoformat()
            # Keep for 60s after resolution for the waiting task to pick up
            await self._client.setex(key, 60, json.dumps(data))
        return data

    # ── Rate limiting ─────────────────────────────────────────────────────────

    async def check_rate_limit(self, user_id: str, limit: int = 30, window: int = 60) -> bool:
        """Returns True if request is allowed, False if rate limited."""
        key = f"rate:{user_id}"
        count = await self._client.incr(key)
        if count == 1:
            await self._client.expire(key, window)
        return count <= limit

    # ── Active task tracking ──────────────────────────────────────────────────

    async def set_active_task(self, user_id: str, task_id: str):
        key = f"active_task:{user_id}"
        await self._client.setex(key, 3600, task_id)

    async def get_active_task(self, user_id: str) -> Optional[str]:
        return await self._client.get(f"active_task:{user_id}")

    async def clear_active_task(self, user_id: str):
        await self._client.delete(f"active_task:{user_id}")


class PostgresMemory:
    """
    Long-term memory: user profiles, task history, preferences.
    Uses SQLAlchemy async for non-blocking DB operations.
    """

    def __init__(self, database_url: str):
        self.database_url = database_url
        self._engine = None
        self._sessionmaker = None

    async def connect(self):
        try:
            from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
            from sqlalchemy.orm import sessionmaker

            self._engine = create_async_engine(
                self.database_url,
                pool_pre_ping=True,
                pool_size=10,
                max_overflow=20,
            )
            self._sessionmaker = sessionmaker(
                self._engine, class_=AsyncSession, expire_on_commit=False
            )
            await self._create_tables()
            logger.info("✅ PostgreSQL connected")
        except Exception as e:
            logger.error(f"❌ PostgreSQL connection failed: {e}")
            logger.warning("Running without persistent memory (Redis only)")

    async def _create_tables(self):
     from sqlalchemy import text
     async with self._engine.begin() as conn:
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS users (
                user_id        TEXT PRIMARY KEY,
                channel        TEXT NOT NULL,
                name           TEXT,
                phone          TEXT,
                persona        TEXT DEFAULT 'aria',
                language       TEXT DEFAULT 'en',
                timezone       TEXT DEFAULT 'Asia/Kolkata',
                city           TEXT,
                preferences    JSONB DEFAULT '{}',
                connected_apps JSONB DEFAULT '[]',
                created_at     TIMESTAMPTZ DEFAULT NOW(),
                last_seen      TIMESTAMPTZ DEFAULT NOW()
            )
        """))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS tasks (
                task_id        TEXT PRIMARY KEY,
                user_id        TEXT REFERENCES users(user_id),
                task_type      TEXT NOT NULL,
                channel        TEXT NOT NULL,
                input_text     TEXT,
                final_answer   TEXT,
                model_used     TEXT,
                latency_ms     INTEGER,
                status         TEXT DEFAULT 'pending',
                needs_approval BOOLEAN DEFAULT FALSE,
                approval_status TEXT DEFAULT 'not_needed',
                tool_calls     JSONB DEFAULT '[]',
                created_at     TIMESTAMPTZ DEFAULT NOW(),
                completed_at   TIMESTAMPTZ
            )
        """))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS memories (
                id             SERIAL PRIMARY KEY,
                user_id        TEXT REFERENCES users(user_id),
                memory_type    TEXT,
                key            TEXT,
                value          TEXT,
                confidence     FLOAT DEFAULT 1.0,
                source_task_id TEXT,
                created_at     TIMESTAMPTZ DEFAULT NOW(),
                updated_at     TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE(user_id, key)
            )
        """))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS idx_tasks_user ON tasks(user_id)"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at)"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS idx_memories_user ON memories(user_id)"))

        
    async def get_or_create_user(self, user_id: str, channel: str, name: str = "") -> dict:
        """Get existing user or create new one."""
        if not self._sessionmaker:
            return {"user_id": user_id, "name": name, "persona": "aria", "preferences": {}}

        from sqlalchemy import text
        async with self._sessionmaker() as session:
            result = await session.execute(
                text("SELECT * FROM users WHERE user_id = :uid"),
                {"uid": user_id}
            )
            row = result.mappings().first()

            if not row:
                await session.execute(
                    text("""INSERT INTO users (user_id, channel, name)
                            VALUES (:uid, :ch, :name)
                            ON CONFLICT DO NOTHING"""),
                    {"uid": user_id, "ch": channel, "name": name}
                )
                await session.commit()
                return {"user_id": user_id, "name": name, "persona": "aria",
                        "preferences": {}, "connected_apps": []}

            return dict(row)

    async def update_user_persona(self, user_id: str, persona: str):
        if not self._sessionmaker:
            return
        from sqlalchemy import text
        async with self._sessionmaker() as session:
            await session.execute(
                text("UPDATE users SET persona = :p WHERE user_id = :uid"),
                {"p": persona, "uid": user_id}
            )
            await session.commit()

    async def save_task(self, task_data: dict):
        """Persist a completed task for history."""
        if not self._sessionmaker:
            return
        from sqlalchemy import text
        async with self._sessionmaker() as session:
            await session.execute(
                text("""INSERT INTO tasks
                    (task_id, user_id, task_type, channel, input_text,
                     final_answer, model_used, latency_ms, status,
                     needs_approval, approval_status, tool_calls, completed_at)
                    VALUES
                    (:task_id, :user_id, :task_type, :channel, :input_text,
                     :final_answer, :model_used, :latency_ms, :status,
                     :needs_approval, :approval_status, :tool_calls::jsonb, NOW())
                    ON CONFLICT (task_id) DO UPDATE SET
                        status = EXCLUDED.status,
                        final_answer = EXCLUDED.final_answer,
                        completed_at = NOW()
                """),
                {
                    "task_id": task_data.get("task_id", str(uuid4())),
                    "user_id": task_data.get("user_id"),
                    "task_type": task_data.get("task_type", "unknown"),
                    "channel": task_data.get("channel", "telegram"),
                    "input_text": task_data.get("input_text", ""),
                    "final_answer": task_data.get("final_answer", ""),
                    "model_used": task_data.get("model_used", ""),
                    "latency_ms": task_data.get("latency_ms", 0),
                    "status": task_data.get("status", "done"),
                    "needs_approval": task_data.get("needs_approval", False),
                    "approval_status": task_data.get("approval_status", "not_needed"),
                    "tool_calls": json.dumps(task_data.get("tool_calls", [])),
                }
            )
            await session.commit()

    async def save_memory(self, user_id: str, key: str, value: str,
                          memory_type: str = "preference"):
        """Store a long-term memory fact about the user."""
        if not self._sessionmaker:
            return
        from sqlalchemy import text
        async with self._sessionmaker() as session:
            await session.execute(
                text("""INSERT INTO memories (user_id, memory_type, key, value)
                        VALUES (:uid, :mt, :k, :v)
                        ON CONFLICT (user_id, key)
                        DO UPDATE SET value = :v, updated_at = NOW()"""),
                {"uid": user_id, "mt": memory_type, "k": key, "v": value}
            )
            await session.commit()

    async def get_user_memories(self, user_id: str) -> dict:
        """Get all stored facts about a user."""
        if not self._sessionmaker:
            return {}
        from sqlalchemy import text
        async with self._sessionmaker() as session:
            result = await session.execute(
                text("SELECT key, value FROM memories WHERE user_id = :uid"),
                {"uid": user_id}
            )
            return {row.key: row.value for row in result}

    async def get_task_history(self, user_id: str, limit: int = 10) -> list[dict]:
        """Get recent task history for context."""
        if not self._sessionmaker:
            return []
        from sqlalchemy import text
        async with self._sessionmaker() as session:
            result = await session.execute(
                text("""SELECT task_type, input_text, final_answer, created_at
                        FROM tasks WHERE user_id = :uid
                        ORDER BY created_at DESC LIMIT :lim"""),
                {"uid": user_id, "lim": limit}
            )
            return [dict(row._mapping) for row in result]


class OwlMemory:
    """
    Unified memory interface — combines Redis (fast) and PostgreSQL (persistent).
    All agent nodes use this single interface.
    """

    def __init__(self, redis_url: str, database_url: str):
        self.redis = RedisMemory(redis_url)
        self.postgres = PostgresMemory(database_url)

    async def connect(self):
        await self.redis.connect()
        await self.postgres.connect()

    async def load_user_context(self, user_id: str, channel: str, name: str = "") -> dict:
        """
        Load complete context for a user at the start of a task.
        Combines Redis (fast, recent) + PostgreSQL (deep, long-term).
        """
        # Get or create user profile
        user_profile = await self.postgres.get_or_create_user(user_id, channel, name)

        # Load stored memories (preferences, facts)
        memories = await self.postgres.get_user_memories(user_id)

        # Load recent conversation history
        history = await self.redis.get_history(user_id)

        # Load active session data
        session = await self.redis.get_session(user_id)

        return {
            "user_id": user_id,
            "name": user_profile.get("name") or name,
            "persona": session.get("persona") or user_profile.get("persona", "aria"),
            "language": user_profile.get("language", "en"),
            "timezone": user_profile.get("timezone", "Asia/Kolkata"),
            "city": user_profile.get("city", ""),
            "preferences": memories,
            "connected_apps": user_profile.get("connected_apps", []),
            "recent_history": history[-10:],  # last 10 messages
        }

    async def save_interaction(self, user_id: str, user_msg: str,
                               assistant_msg: str, task_data: dict):
        """Save a completed interaction to both stores."""
        now = datetime.utcnow().isoformat()

        # Save to Redis history buffer
        await self.redis.append_message(user_id, {
            "role": "user", "content": user_msg, "timestamp": now
        })
        await self.redis.append_message(user_id, {
            "role": "assistant", "content": assistant_msg, "timestamp": now
        })

        # Save task to PostgreSQL
        await self.postgres.save_task({
            **task_data,
            "user_id": user_id,
            "input_text": user_msg,
            "final_answer": assistant_msg,
        })
