"""Memory stores backed by the current file layout and a stronger semantic fact store."""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from pathlib import Path
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)


SummaryLoader = Callable[[uuid.UUID, str | None], Awaitable[str | None]]
MemoryLoader = Callable[[uuid.UUID], str]


# Valid memory categories — extended for unified evolution schema (P1.2).
# Core 4 (Claude Code aligned) + evolution types + general default.
MEMORY_CATEGORIES = frozenset(
    {
        "user",  # preferences, role, knowledge, working style
        "feedback",  # corrections, confirmations, behavioral guidance
        "project",  # goals, deadlines, decisions, status updates
        "reference",  # pointers to external systems, URLs, tool names
        "general",  # anything else
        "constraint",  # hard rules agent must follow
        "strategy",  # successful approaches worth reusing
        "blocked_pattern",  # approaches proven to fail — do not retry
    }
)


class PersistentMemoryStore:
    """Persistent semantic fact store backed by a per-agent SQLite database.

    The store remains compatible with the previous `memory.json` format by:
    1. importing legacy facts on first access
    2. exporting the canonical fact list back to `memory.json`
    """

    def __init__(self, *, data_root: Path) -> None:
        self.data_root = Path(data_root)

    def replace_semantic_facts(self, agent_id: uuid.UUID, facts: list[dict]) -> None:
        facts = [fact for fact in facts if isinstance(fact, dict) and fact.get("content")]
        with self._connect(agent_id) as conn:
            self._ensure_schema(conn)
            conn.execute("DELETE FROM semantic_facts")
            rows = []
            for index, fact in enumerate(facts):
                payload = dict(fact)
                content = str(payload.pop("content"))[:2000]  # Cap individual fact content
                subject = payload.pop("subject", None)
                timestamp = payload.pop("timestamp", payload.pop("created_at", None))
                category = payload.pop("category", "general")
                if category not in MEMORY_CATEGORIES:
                    category = "general"
                importance = float(payload.pop("importance", 0.5))
                rows.append(
                    (
                        index,
                        content,
                        str(subject) if subject else None,
                        str(timestamp) if timestamp else None,
                        json.dumps(payload, ensure_ascii=False) if payload else None,
                        category,
                        importance,
                    )
                )
            conn.executemany(
                """
                INSERT INTO semantic_facts(position, content, subject, timestamp, metadata_json, category, importance)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            # Rebuild FTS index
            self._rebuild_fts(conn)
            # Write legacy JSON INSIDE the connection context so both succeed or
            # the DB commit is the canonical source (BP-E fix).
            try:
                self._write_legacy_json(agent_id, facts)
            except Exception as json_err:
                logger.warning("[MemoryStore] Legacy JSON write failed (DB committed): %s", json_err)
            conn.commit()

    def load_semantic_facts(self, agent_id: uuid.UUID, *, limit: int | None = None) -> list[dict]:
        with self._connect(agent_id) as conn:
            self._ensure_schema(conn)
            self._import_legacy_json_if_needed(agent_id, conn)
            query = (
                "SELECT content, subject, timestamp, metadata_json, category, importance FROM semantic_facts "
                "ORDER BY position ASC"
            )
            params: tuple[object, ...] = ()
            if limit is not None:
                query += " LIMIT ?"
                params = (int(limit),)
            rows = conn.execute(query, params).fetchall()

        facts: list[dict] = []
        for row in rows:
            content, subject, timestamp, metadata_json = row[0], row[1], row[2], row[3]
            category = row[4] if len(row) > 4 else "general"
            importance = row[5] if len(row) > 5 else 0.5
            fact: dict = {"content": content, "category": category or "general", "importance": importance}
            if subject:
                fact["subject"] = subject
            if timestamp:
                fact["timestamp"] = timestamp
            if metadata_json:
                try:
                    payload = json.loads(metadata_json)
                    if isinstance(payload, dict):
                        fact.update(payload)
                except json.JSONDecodeError as _jde2:
                    logger.debug("[MemoryStore] Malformed metadata in search result: %s", _jde2)
                    fact["metadata_json"] = metadata_json
            facts.append(fact)
        return facts

    def render_semantic_lines(self, agent_id: uuid.UUID, *, limit: int = 30) -> str:
        lines = []
        for fact in self.load_semantic_facts(agent_id):
            content = fact.get("content", fact.get("fact", ""))
            if content:
                lines.append(f"- {content}")
        return "\n".join(lines[-limit:])

    def _memory_dir(self, agent_id: uuid.UUID) -> Path:
        return self.data_root / str(agent_id) / "memory"

    def _db_path(self, agent_id: uuid.UUID) -> Path:
        return self._memory_dir(agent_id) / "memory.sqlite3"

    def _legacy_json_path(self, agent_id: uuid.UUID) -> Path:
        return self._memory_dir(agent_id) / "memory.json"

    def _connect(self, agent_id: uuid.UUID) -> sqlite3.Connection:
        memory_dir = self._memory_dir(agent_id)
        memory_dir.mkdir(parents=True, exist_ok=True)
        db_path = self._db_path(agent_id)
        try:
            conn = sqlite3.connect(db_path)
            # WAL mode for concurrent read/write safety; busy_timeout prevents
            # immediate "database is locked" errors on concurrent writes.
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            # Quick integrity check — detect corruption early
            conn.execute("PRAGMA integrity_check").fetchone()
            return conn
        except sqlite3.DatabaseError as exc:
            logger.warning("[MemoryStore] SQLite corrupted for agent %s, recovering: %s", agent_id, exc)
            # Verify legacy JSON exists and is valid BEFORE destroying the DB
            json_path = self._legacy_json_path(agent_id)
            json_ok = False
            if json_path.exists():
                try:
                    payload = json.loads(json_path.read_text(encoding="utf-8"))
                    json_ok = isinstance(payload, list) and len(payload) > 0
                except Exception as json_err:
                    logger.warning("[MemoryStore] Legacy JSON also invalid for %s: %s", agent_id, json_err)
            if not json_ok:
                # Both stores corrupted — backup DB but warn about total memory loss
                logger.error(
                    "[MemoryStore] BOTH SQLite and JSON corrupted for agent %s. "
                    "Memory will be lost. Corrupted DB preserved for manual recovery.",
                    agent_id,
                )
            # Backup corrupted DB file
            corrupted = db_path.with_suffix(".sqlite3.corrupted")
            try:
                import shutil

                shutil.move(str(db_path), str(corrupted))
                logger.info("[MemoryStore] Corrupted DB backed up to %s", corrupted)
            except OSError as mv_err:
                logger.warning("[MemoryStore] Failed to backup corrupted DB: %s", mv_err)
                db_path.unlink(missing_ok=True)
            # Recreate fresh connection — legacy JSON will be re-imported if valid
            return sqlite3.connect(db_path)

    def search_facts(self, agent_id: uuid.UUID, query: str, *, limit: int = 5) -> list[dict]:
        """Full-text search over semantic facts using FTS5. Falls back to LIKE if FTS unavailable."""
        if not query or not query.strip():
            return self.load_semantic_facts(agent_id, limit=limit)

        with self._connect(agent_id) as conn:
            self._ensure_schema(conn)
            self._import_legacy_json_if_needed(agent_id, conn)

            # Try FTS5 first (standalone table, joined back to main for metadata)
            try:
                rows = conn.execute(
                    """
                    SELECT sf.content, sf.subject, sf.timestamp, sf.metadata_json, sf.category, sf.importance
                    FROM semantic_facts_fts fts
                    JOIN semantic_facts sf ON sf.rowid = fts.rowid
                    WHERE fts MATCH ?
                    ORDER BY fts.rank
                    LIMIT ?
                    """,
                    (query, limit),
                ).fetchall()
            except sqlite3.OperationalError as exc:
                # FTS table missing or corrupt — fall back to LIKE
                logger.info("[MemoryStore] FTS5 search failed, using LIKE fallback: %s", exc)
                like_pattern = f"%{query}%"
                rows = conn.execute(
                    """
                    SELECT content, subject, timestamp, metadata_json, category, importance
                    FROM semantic_facts
                    WHERE content LIKE ? OR subject LIKE ?
                    ORDER BY position DESC
                    LIMIT ?
                    """,
                    (like_pattern, like_pattern, limit),
                ).fetchall()

        facts: list[dict] = []
        for row in rows:
            content, subject, timestamp, metadata_json = row[0], row[1], row[2], row[3]
            category = row[4] if len(row) > 4 else "general"
            importance = row[5] if len(row) > 5 else 0.5
            fact: dict = {"content": content, "category": category or "general", "importance": importance}
            if subject:
                fact["subject"] = subject
            if timestamp:
                fact["timestamp"] = timestamp
            if metadata_json:
                try:
                    payload = json.loads(metadata_json)
                    if isinstance(payload, dict):
                        fact.update(payload)
                except json.JSONDecodeError as _jde:
                    logger.debug("[MemoryStore] Malformed metadata_json: %s", _jde)
            facts.append(fact)
        return facts

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS semantic_facts (
                position INTEGER NOT NULL,
                content TEXT NOT NULL,
                subject TEXT,
                timestamp TEXT,
                metadata_json TEXT,
                category TEXT DEFAULT 'general',
                importance REAL DEFAULT 0.5
            )
            """
        )
        # Migrate existing DBs: add missing columns
        cols = {row[1] for row in conn.execute("PRAGMA table_info(semantic_facts)").fetchall()}
        if "category" not in cols:
            conn.execute("ALTER TABLE semantic_facts ADD COLUMN category TEXT DEFAULT 'general'")
        if "importance" not in cols:
            conn.execute("ALTER TABLE semantic_facts ADD COLUMN importance REAL DEFAULT 0.5")
        # Standalone FTS5 table — kept in sync manually via _rebuild_fts()
        try:
            conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS semantic_facts_fts USING fts5(content, subject)")
        except sqlite3.OperationalError:
            logger.warning("[MemoryStore] FTS5 not available on this SQLite build, falling back to LIKE search")

    def _import_legacy_json_if_needed(self, agent_id: uuid.UUID, conn: sqlite3.Connection) -> None:
        row = conn.execute("SELECT COUNT(*) FROM semantic_facts").fetchone()
        if row and row[0]:
            return

        memory_file = self._legacy_json_path(agent_id)
        if not memory_file.exists():
            return

        try:
            facts = json.loads(memory_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as _legacy_err:
            logger.debug("[MemoryStore] Legacy JSON import failed: %s", _legacy_err)
            return
        if not isinstance(facts, list):
            return
        conn.execute("DELETE FROM semantic_facts")
        rows = []
        for index, fact in enumerate(facts):
            if not isinstance(fact, dict) or not fact.get("content"):
                continue
            payload = dict(fact)
            content = str(payload.pop("content"))
            subject = payload.pop("subject", None)
            timestamp = payload.pop("timestamp", payload.pop("created_at", None))
            category = payload.pop("category", "general")
            if category not in MEMORY_CATEGORIES:
                category = "general"
            rows.append(
                (
                    index,
                    content,
                    str(subject) if subject else None,
                    str(timestamp) if timestamp else None,
                    json.dumps(payload, ensure_ascii=False) if payload else None,
                    category,
                )
            )
        conn.executemany(
            """
            INSERT INTO semantic_facts(position, content, subject, timestamp, metadata_json, category)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        self._rebuild_fts(conn)
        conn.commit()
        self._write_legacy_json(agent_id, facts)

    def _rebuild_fts(self, conn: sqlite3.Connection) -> None:
        """Rebuild standalone FTS5 index from semantic_facts. No-op if FTS5 unavailable."""
        try:
            conn.execute("DELETE FROM semantic_facts_fts")
            conn.execute(
                """
                INSERT INTO semantic_facts_fts(rowid, content, subject)
                SELECT rowid, content, COALESCE(subject, '') FROM semantic_facts
                """
            )
        except (sqlite3.OperationalError, sqlite3.DatabaseError) as exc:
            logger.warning("[MemoryStore] FTS5 rebuild failed: %s", exc)

    def _write_legacy_json(self, agent_id: uuid.UUID, facts: list[dict]) -> None:
        memory_file = self._legacy_json_path(agent_id)
        memory_file.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write: write to temp file then rename to prevent corruption on crash
        import os
        import tempfile

        tmp_fd, tmp_path = tempfile.mkstemp(dir=str(memory_file.parent), suffix=".tmp")
        fd_closed = False
        try:
            os.write(tmp_fd, json.dumps(facts, ensure_ascii=False, indent=2).encode("utf-8"))
            os.close(tmp_fd)
            fd_closed = True
            os.replace(tmp_path, str(memory_file))
        except BaseException:
            if not fd_closed:
                os.close(tmp_fd)
            try:
                os.unlink(tmp_path)
            except OSError as unlink_err:
                logger.warning("[MemoryStore] Failed to clean up temp file %s: %s", tmp_path, unlink_err)
            raise


class FileBackedMemoryStore:
    """Build runtime memory context from the current Hive storage layout."""

    def __init__(
        self,
        *,
        data_root: Path,
        load_session_summary: SummaryLoader,
        load_previous_session_summary: SummaryLoader,
        load_agent_memory: MemoryLoader | None = None,
    ) -> None:
        self.data_root = Path(data_root)
        self.load_session_summary = load_session_summary
        self.load_previous_session_summary = load_previous_session_summary
        self._persistent_store = PersistentMemoryStore(data_root=self.data_root)
        self.load_agent_memory = load_agent_memory or self._load_agent_memory

    async def build_context(
        self,
        *,
        agent_id: uuid.UUID,
        tenant_id: uuid.UUID,
        session_id: str | None = None,
    ) -> str:
        del tenant_id  # reserved for future backends

        parts: list[str] = []
        if session_id:
            current_summary = await self.load_session_summary(agent_id, session_id)
            if current_summary:
                parts.append(f"[Previous conversation summary]\n{current_summary}")
            else:
                previous_summary = await self.load_previous_session_summary(agent_id, session_id)
                if previous_summary:
                    parts.append(f"[Previous conversation summary]\n{previous_summary}")

        memory_text = self.load_agent_memory(agent_id)
        if memory_text:
            parts.append(f"[Agent memory]\n{memory_text}")

        return "\n\n".join(parts)

    async def build_session_snapshot(
        self,
        *,
        agent_id: uuid.UUID,
        tenant_id: uuid.UUID,
        session_id: str | None = None,
    ) -> str:
        """Build a frozen memory snapshot for session-start prompt caching."""
        return await self.build_context(
            agent_id=agent_id,
            tenant_id=tenant_id,
            session_id=session_id,
        )

    def _load_agent_memory(self, agent_id: uuid.UUID) -> str:
        return self._persistent_store.render_semantic_lines(agent_id)
