"""
RecoveryOS — Database Session Factory
======================================
Provides async SQLAlchemy engine + session factory.
Also exposes a synchronous engine for Alembic migrations.

Connection roles:
  - get_app_session()       → uses app_role (full R/W, except audit_log/events no DELETE/UPDATE)
  - get_diagnoser_session() → uses diagnoser_role (SELECT only, no ground_truth columns)
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy import create_engine, text

from recoveryos.config import get_settings


def _build_async_engine(url: str):
    return create_async_engine(
        url,
        pool_size=10,
        max_overflow=20,
        pool_pre_ping=True,
        echo=False,
    )


def _build_sync_engine(url: str):
    return create_engine(url, pool_pre_ping=True)


# ─── Application engine (app_role) ───────────────────────────────────────────
_app_engine = None
_app_session_factory = None

# ─── Diagnoser engine (diagnoser_role — read-only, no ground_truth) ──────────
_diagnoser_engine = None
_diagnoser_session_factory = None


def get_app_engine():
    global _app_engine
    if _app_engine is None:
        _app_engine = _build_async_engine(get_settings().database_url)
    return _app_engine


def get_diagnoser_engine():
    global _diagnoser_engine
    if _diagnoser_engine is None:
        _diagnoser_engine = _build_async_engine(get_settings().diagnoser_database_url)
    return _diagnoser_engine


def get_app_session_factory() -> async_sessionmaker[AsyncSession]:
    global _app_session_factory
    if _app_session_factory is None:
        _app_session_factory = async_sessionmaker(
            bind=get_app_engine(),
            expire_on_commit=False,
            autoflush=False,
        )
    return _app_session_factory


def get_diagnoser_session_factory() -> async_sessionmaker[AsyncSession]:
    global _diagnoser_session_factory
    if _diagnoser_session_factory is None:
        _diagnoser_session_factory = async_sessionmaker(
            bind=get_diagnoser_engine(),
            expire_on_commit=False,
            autoflush=False,
        )
    return _diagnoser_session_factory


async def get_app_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency — yields an app_role session."""
    async with get_app_session_factory()() as session:
        yield session


async def get_diagnoser_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency — yields a diagnoser_role session (read-only, no ground_truth)."""
    async with get_diagnoser_session_factory()() as session:
        yield session
