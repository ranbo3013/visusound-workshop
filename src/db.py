"""SQLite 数据层 — 声画工坊工作台持久化。

本地优先：数据库文件位于 <项目根>/data/workshop.db。
提供 4 张表：projects / tasks / settings / voices，以及对应的 CRUD 辅助。
任务队列（tasks 表）支持后台异步执行 + 进度持久化，供批量配音 / 批量处理等模块使用。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "data" / "workshop.db"
_lock = threading.Lock()

# WAL 模式提升并发读写性能；check_same_thread=False 允许后台线程访问。
def get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def init_db() -> None:
    """创建表结构（幂等）。应用启动时调用一次。"""
    conn = get_conn()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS projects (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL,
                description TEXT DEFAULT '',
                status      TEXT DEFAULT 'active',
                created_at  TEXT,
                updated_at  TEXT
            );
            CREATE TABLE IF NOT EXISTS tasks (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                type        TEXT NOT NULL,
                status      TEXT DEFAULT 'pending',
                progress    INTEGER DEFAULT 0,
                payload     TEXT DEFAULT '{}',
                result      TEXT DEFAULT '{}',
                error       TEXT DEFAULT '',
                created_at  TEXT,
                updated_at  TEXT
            );
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            );
            CREATE TABLE IF NOT EXISTS voices (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL,
                gender      TEXT DEFAULT '',
                tags        TEXT DEFAULT '',
                provider    TEXT DEFAULT '',
                meta        TEXT DEFAULT '{}',
                created_at  TEXT
            );
            """
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------

def get_setting(key: str, default=None):
    conn = get_conn()
    try:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default
    finally:
        conn.close()


def set_setting(key: str, value) -> None:
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
        conn.commit()
    finally:
        conn.close()


def get_all_settings() -> dict:
    conn = get_conn()
    try:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
        return {r["key"]: r["value"] for r in rows}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# projects
# ---------------------------------------------------------------------------

def list_projects() -> list[dict]:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM projects ORDER BY updated_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def create_project(name: str, description: str = "", status: str = "active") -> int:
    conn = get_conn()
    try:
        now = _now()
        cur = conn.execute(
            "INSERT INTO projects(name, description, status, created_at, updated_at) "
            "VALUES(?, ?, ?, ?, ?)",
            (name, description, status, now, now),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def update_project(pid: int, **fields) -> None:
    if not fields:
        return
    fields["updated_at"] = _now()
    cols = ", ".join(f"{k}=?" for k in fields)
    conn = get_conn()
    try:
        conn.execute(f"UPDATE projects SET {cols} WHERE id=?", (*fields.values(), pid))
        conn.commit()
    finally:
        conn.close()


def delete_project(pid: int) -> None:
    conn = get_conn()
    try:
        conn.execute("DELETE FROM projects WHERE id=?", (pid,))
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# tasks（异步任务队列）
# ---------------------------------------------------------------------------

def create_task(type_: str, payload: dict | None = None) -> int:
    conn = get_conn()
    try:
        now = _now()
        cur = conn.execute(
            "INSERT INTO tasks(type, status, progress, payload, created_at, updated_at) "
            "VALUES(?, ?, ?, ?, ?, ?)",
            (type_, "pending", 0, json.dumps(payload or {}), now, now),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def update_task(
    tid: int,
    status: str | None = None,
    progress: int | None = None,
    result: dict | None = None,
    error: str | None = None,
) -> None:
    conn = get_conn()
    try:
        sets, vals = [], []
        if status is not None:
            sets.append("status=?"); vals.append(status)
        if progress is not None:
            sets.append("progress=?"); vals.append(progress)
        if result is not None:
            sets.append("result=?"); vals.append(json.dumps(result))
        if error is not None:
            sets.append("error=?"); vals.append(error)
        sets.append("updated_at=?"); vals.append(_now())
        conn.execute(f"UPDATE tasks SET {','.join(sets)} WHERE id=?", (*vals, tid))
        conn.commit()
    finally:
        conn.close()


def get_task(tid: int) -> dict | None:
    conn = get_conn()
    try:
        r = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def list_tasks(limit: int = 50) -> list[dict]:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# voices（声音库元数据）
# ---------------------------------------------------------------------------

def list_voices() -> list[dict]:
    conn = get_conn()
    try:
        rows = conn.execute("SELECT * FROM voices ORDER BY id").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def create_voice(name: str, gender: str = "", tags: str = "", provider: str = "", meta: dict | None = None) -> int:
    conn = get_conn()
    try:
        now = _now()
        cur = conn.execute(
            "INSERT INTO voices(name, gender, tags, provider, meta, created_at) "
            "VALUES(?, ?, ?, ?, ?, ?)",
            (name, gender, tags, provider, json.dumps(meta or {}), now),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()
