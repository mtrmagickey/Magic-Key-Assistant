"""Shared SQLite helpers for small CRUD-style services."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator


class SqliteService:
    """Small base class that removes repetitive connection and row plumbing."""

    def __init__(self, db: Any):
        self.db = db

    @asynccontextmanager
    async def _acquire(self) -> AsyncIterator[Any]:
        if hasattr(self.db, "acquire"):
            async with self.db.acquire() as conn:
                yield conn
            return

        conn = getattr(self.db, "connection", None)
        if conn is None:
            raise RuntimeError("Service requires a db with 'acquire()' or 'connection'")
        yield conn

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[Any]:
        async with self._acquire() as conn:
            try:
                yield conn
                await conn.commit()
            except Exception:
                rollback = getattr(conn, "rollback", None)
                if rollback is not None:
                    await rollback()
                raise

    @staticmethod
    def _row_to_dict(row: Any) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    async def _fetchone(
        self,
        query: str,
        params: tuple[Any, ...] = (),
        *,
        conn: Any | None = None,
    ) -> dict[str, Any] | None:
        if conn is None:
            async with self._acquire() as owned_conn:
                return await self._fetchone(query, params, conn=owned_conn)

        async with conn.execute(query, params) as cursor:
            return self._row_to_dict(await cursor.fetchone())

    async def _fetchall(
        self,
        query: str,
        params: tuple[Any, ...] = (),
        *,
        conn: Any | None = None,
    ) -> list[dict[str, Any]]:
        if conn is None:
            async with self._acquire() as owned_conn:
                return await self._fetchall(query, params, conn=owned_conn)

        async with conn.execute(query, params) as cursor:
            return [dict(row) for row in await cursor.fetchall()]

    async def _insert(
        self,
        query: str,
        params: tuple[Any, ...] = (),
        *,
        conn: Any | None = None,
    ) -> int:
        if conn is None:
            async with self._transaction() as owned_conn:
                return await self._insert(query, params, conn=owned_conn)

        async with conn.execute(query, params) as cursor:
            return int(cursor.lastrowid or 0)

    async def _execute(
        self,
        query: str,
        params: tuple[Any, ...] = (),
        *,
        conn: Any | None = None,
    ) -> int:
        if conn is None:
            async with self._transaction() as owned_conn:
                return await self._execute(query, params, conn=owned_conn)

        async with conn.execute(query, params) as cursor:
            return int(cursor.rowcount or 0)