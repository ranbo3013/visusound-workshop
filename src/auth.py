"""用户认证 — 声画工坊。

零依赖实现（仅用标准库）：
- 密码哈希：hashlib.pbkdf2_hmac(sha256, 随机盐, 100000 迭代)
- 会话：session_id 存 SQLite（sessions 表），默认 7 天过期
- 角色：admin / user；首注册用户自动成为 admin
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta

from . import db

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

SESSION_COOKIE = "session_id"
SESSION_MAX_AGE = 60 * 60 * 24 * 7  # 7 天（秒）
PBKDF2_ITERS = 100_000


# ---------------------------------------------------------------------------
# 密码哈希
# ---------------------------------------------------------------------------

def hash_password(password: str) -> str:
    """返回 pbkdf2_sha256$<iters>$<salt_hex>$<hash_hex>。"""
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                             bytes.fromhex(salt), PBKDF2_ITERS)
    return f"pbkdf2_sha256${PBKDF2_ITERS}${salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """校验明文密码与存储的哈希是否匹配（常量时间比较）。"""
    try:
        algo, iters, salt, expected = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"),
            bytes.fromhex(salt), int(iters),
        )
        return secrets.compare_digest(dk.hex(), expected)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 会话管理
# ---------------------------------------------------------------------------

def create_session(user_id: int) -> str:
    """生成会话，写入 sessions 表，返回 session_id。"""
    sid = secrets.token_urlsafe(32)
    expires = (datetime.now() + timedelta(seconds=SESSION_MAX_AGE)).isoformat(timespec="seconds")
    db.create_session(sid, user_id, expires)
    return sid


def get_session_user(session_id: str | None) -> dict | None:
    """根据 session_id 返回用户字典（含 role/status）；无效或过期返回 None。"""
    if not session_id:
        return None
    s = db.get_session(session_id)
    if not s:
        return None
    # 过期检查
    if s.get("expires_at"):
        try:
            exp = datetime.fromisoformat(s["expires_at"])
            if exp < datetime.now():
                db.delete_session(session_id)
                return None
        except Exception:
            pass
    user = db.get_user(s["user_id"])
    if not user:
        return None
    # 被禁用账号立即失效
    if user.get("status") != "active":
        return None
    return user


def delete_session(session_id: str | None) -> None:
    if session_id:
        db.delete_session(session_id)


# ---------------------------------------------------------------------------
# 注册辅助
# ---------------------------------------------------------------------------

def register_user(username: str, password: str, display_name: str = "",
                  role: str | None = None) -> dict:
    """创建用户。role 不传时：首用户=admin，其余=user。返回用户字典（不含密码哈希）。"""
    if role is None:
        role = "admin" if db.count_users() == 0 else "user"
    uid = db.create_user(username, hash_password(password), role=role,
                         display_name=display_name or username)
    user = db.get_user(uid)
    return _public_user(user)


def _public_user(user: dict | None) -> dict | None:
    """去掉 password_hash 后的安全用户字典。"""
    if not user:
        return None
    return {k: v for k, v in user.items() if k != "password_hash"}
