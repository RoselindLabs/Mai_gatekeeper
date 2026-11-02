"""麦麦守门人（Mai_gatekeeper）SQLite 持久化。

两张表：
- blocked_users：屏蔽名单。scope 为 ``global``（全局）或 ``group:<群号>``（仅该群）。
- friend_requests：好友申请审批队列（QQ 的申请 flag 有时效，需落库等待管理员处理）。

拦截 Hook 每条消息都会经过，因此热路径只查内存缓存，写操作后刷新缓存。
db.* 能力只能操作主程序预定义表（无建表通道），插件私有数据按官方推荐
存放在 ctx.paths.data_dir 下自建 SQLite。
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS blocked_users (
    platform   TEXT NOT NULL,
    user_id    TEXT NOT NULL,
    scope      TEXT NOT NULL,
    reason     TEXT NOT NULL DEFAULT '',
    blocked_by TEXT NOT NULL DEFAULT '',
    blocked_at TEXT NOT NULL,
    expires_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (platform, user_id, scope)
);
CREATE TABLE IF NOT EXISTS friend_requests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    flag        TEXT NOT NULL UNIQUE,
    user_id     TEXT NOT NULL,
    nickname    TEXT NOT NULL DEFAULT '',
    comment     TEXT NOT NULL DEFAULT '',
    received_at TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',
    decided_by  TEXT NOT NULL DEFAULT '',
    decided_at  TEXT NOT NULL DEFAULT '',
    decision    TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_friend_requests_status ON friend_requests(status);
"""

GLOBAL_SCOPE = "global"


def group_scope(group_id: str) -> str:
    """构造群级屏蔽作用域标记。"""
    return f"group:{group_id}"


def _parse_expiry(expires_at: Any) -> float:
    """把 ISO 到期时间解析为 unix 时间戳；空/无效视为永久（0）。"""
    text = str(expires_at or "").strip()
    if not text:
        return 0.0
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return 0.0


class GateKeeperStore:
    """屏蔽名单与好友申请的持久化存储。"""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        try:
            self._initialize()
        except BaseException:
            self._conn.close()
            raise

    def _initialize(self) -> None:
        db_path = self._db_path
        # Never recreate a corrupt/unsupported database or silently drop old records.
        if self._conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            self._conn.close()
            raise RuntimeError("Gatekeeper database integrity check failed")
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if version > 1:
            self._conn.close()
            raise RuntimeError("Unsupported Gatekeeper database version")
        if version == 0 and self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='blocked_users'"
        ).fetchone():
            backup = db_path.with_name(db_path.name + ".pre-0.6.8.bak")
            if not backup.exists():
                with closing(sqlite3.connect(str(backup))) as destination:
                    self._conn.backup(destination)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.executescript("BEGIN IMMEDIATE;\n" + _SCHEMA)
        # 旧库迁移：补 expires_at 列（planner 群聊屏蔽的到期时间）
        columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(blocked_users)")}
        if "expires_at" not in columns:
            self._conn.execute("ALTER TABLE blocked_users ADD COLUMN expires_at TEXT NOT NULL DEFAULT ''")
        columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(friend_requests)")}
        if "account_id" not in columns:
            self._conn.execute("ALTER TABLE friend_requests ADD COLUMN account_id TEXT NOT NULL DEFAULT ''")
        self._conn.execute("""CREATE TABLE IF NOT EXISTS action_ledger (
            kind TEXT NOT NULL, action_key TEXT NOT NULL, status TEXT NOT NULL,
            updated_at TEXT NOT NULL, PRIMARY KEY(kind, action_key))""")
        self._conn.execute("PRAGMA user_version=1")
        # A crash/cancellation may happen after remote dispatch. Never replay it.
        self._conn.execute("UPDATE friend_requests SET status='unknown' WHERE status='executing'")
        self._conn.execute("UPDATE action_ledger SET status='unknown' WHERE status='executing'")
        # 缓存：(platform, user_id, scope) -> 到期 unix 时间戳（0 = 永久）
        self._cache: dict[tuple[str, str, str], float] = {}
        self.refresh_cache()
        self._conn.execute("COMMIT")

    # ------------------------------------------------------------------
    # 屏蔽名单
    # ------------------------------------------------------------------

    def refresh_cache(self) -> None:
        """从数据库重建内存缓存（写操作后调用）。"""
        rows = self._conn.execute(
            "SELECT platform, user_id, scope, expires_at FROM blocked_users"
        ).fetchall()
        self._cache = {
            (str(r["platform"]), str(r["user_id"]), str(r["scope"])): _parse_expiry(r["expires_at"])
            for r in rows
        }

    def _entry_active(self, key: tuple[str, str, str]) -> bool:
        """缓存条目是否仍然生效（未到期）。"""
        expiry = self._cache.get(key)
        if expiry is None:
            return False
        return expiry <= 0 or expiry > time.time()

    def is_blocked(self, platform: str, user_id: str, group_id: str = "") -> bool:
        """判断用户是否被屏蔽（全局屏蔽或在指定群被屏蔽，未到期）。"""
        if not user_id:
            return False
        platform = platform or "qq"
        if self._entry_active((platform, user_id, GLOBAL_SCOPE)):
            return True
        if group_id and self._entry_active((platform, user_id, group_scope(group_id))):
            return True
        return False

    def is_blocked_any_scope(self, platform: str, user_id: str) -> bool:
        """判断用户是否在任意作用域被屏蔽（未到期）。"""
        if not user_id:
            return False
        platform = platform or "qq"
        return any(
            key[1] == user_id and key[0] == platform and self._entry_active(key)
            for key in self._cache
        )

    def add_blocked(
        self,
        platform: str,
        user_id: str,
        scope: str,
        reason: str = "",
        blocked_by: str = "",
        expires_at: str = "",
    ) -> bool:
        """新增屏蔽记录。expires_at 为 ISO 时间（空 = 永久）。

        已存在同一 (platform, user_id, scope) 且未到期时返回 False；
        旧记录已到期则先清除再写入（planner 重新屏蔽的场景）。
        """
        if not user_id:
            return False
        platform = platform or "qq"
        now_iso = datetime.now().isoformat()
        # 同键旧记录若已到期，先删除以便重新屏蔽（比较值与到期时间同为微秒精度）
        self._conn.execute(
            "DELETE FROM blocked_users"
            " WHERE platform = ? AND user_id = ? AND scope = ?"
            " AND expires_at != '' AND expires_at <= ?",
            (platform, user_id, scope, now_iso),
        )
        try:
            self._conn.execute(
                "INSERT INTO blocked_users"
                " (platform, user_id, scope, reason, blocked_by, blocked_at, expires_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (platform, user_id, scope, reason, blocked_by, now_iso, expires_at),
            )
        except sqlite3.IntegrityError:
            return False
        self.refresh_cache()
        return True

    def purge_expired(self) -> int:
        """删除所有已到期的屏蔽记录，返回删除条数。"""
        now_iso = datetime.now().isoformat()
        cursor = self._conn.execute(
            "DELETE FROM blocked_users WHERE expires_at != '' AND expires_at <= ?", (now_iso,)
        )
        removed = cursor.rowcount
        if removed:
            self.refresh_cache()
        return removed

    def remove_blocked(self, row_id: int) -> bool:
        """按记录 ID 删除屏蔽条目。"""
        cursor = self._conn.execute("DELETE FROM blocked_users WHERE rowid = ?", (row_id,))
        removed = cursor.rowcount > 0
        if removed:
            self.refresh_cache()
        return removed

    def remove_blocked_by_user(self, platform: str, user_id: str, scope: str = "") -> int:
        """删除某用户的部分（指定 scope）或全部屏蔽条目，返回删除条数。"""
        platform = platform or "qq"
        if scope:
            cursor = self._conn.execute(
                "DELETE FROM blocked_users WHERE platform = ? AND user_id = ? AND scope = ?",
                (platform, user_id, scope),
            )
        else:
            cursor = self._conn.execute(
                "DELETE FROM blocked_users WHERE platform = ? AND user_id = ?",
                (platform, user_id),
            )
        removed = cursor.rowcount
        if removed:
            self.refresh_cache()
        return removed

    def list_blocked(self, limit: int = 50) -> list[dict[str, Any]]:
        """列出屏蔽名单（rowid 作为管理命令使用的序号）。"""
        rows = self._conn.execute(
            "SELECT rowid AS id, platform, user_id, scope, reason, blocked_by, blocked_at, expires_at"
            " FROM blocked_users ORDER BY blocked_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # 好友申请
    # ------------------------------------------------------------------

    def add_friend_request(
        self, flag: str, user_id: str, nickname: str = "", comment: str = "", account_id: str = ""
    ) -> int | None:
        """记录一条好友申请。同一 flag 重复收到时返回 None（QQ 会重推事件）。"""
        try:
            self._conn.execute(
                "INSERT INTO friend_requests (flag, user_id, nickname, comment, received_at, status, account_id)"
                " VALUES (?, ?, ?, ?, ?, 'pending', ?)",
                (flag, user_id, nickname, comment, datetime.now().isoformat(timespec="seconds"), account_id),
            )
        except sqlite3.IntegrityError:
            return None
        row = self._conn.execute(
            "SELECT id FROM friend_requests WHERE flag = ?", (flag,)
        ).fetchone()
        return int(row["id"]) if row else None

    def get_friend_request(self, row_id: int) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM friend_requests WHERE id = ?", (row_id,)
        ).fetchone()
        return dict(row) if row else None

    def list_friend_requests(
        self, status: str = "pending", limit: int = 10
    ) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM friend_requests WHERE status = ? ORDER BY id DESC LIMIT ?",
            (status, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def update_friend_request(
        self, row_id: int, status: str, decided_by: str = "", decision: str = ""
    ) -> bool:
        cursor = self._conn.execute(
            "UPDATE friend_requests SET status = ?, decided_by = ?, decided_at = ?, decision = ?"
            " WHERE id = ? AND status = 'pending'",
            (
                status,
                decided_by,
                datetime.now().isoformat(timespec="seconds"),
                decision,
                row_id,
            ),
        )
        return cursor.rowcount > 0

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    def claim_request(self, row_id: int, account_id: str, decided_by: str, decision: str) -> bool:
        if not account_id:
            return False
        cursor = self._conn.execute(
            "UPDATE friend_requests SET status='executing', decided_by=?, decision=?, decided_at=?"
            " WHERE id=? AND status='pending' AND account_id=?",
            (decided_by, decision, datetime.now().isoformat(), row_id, account_id),
        )
        return cursor.rowcount == 1

    def finish_request(self, row_id: int, status: str) -> None:
        cursor = self._conn.execute(
            "UPDATE friend_requests SET status=?, decided_at=? WHERE id=? AND status='executing'",
            (status, datetime.now().isoformat(), row_id),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("Decision claim lost; remote outcome must be reconciled")

    def claim_action(self, kind: str, key: str) -> bool:
        cursor = self._conn.execute(
            "INSERT OR IGNORE INTO action_ledger VALUES (?, ?, 'executing', ?)",
            (kind, key, datetime.now().isoformat()),
        )
        return cursor.rowcount == 1

    def finish_action(self, kind: str, key: str, status: str) -> None:
        cursor = self._conn.execute(
            "UPDATE action_ledger SET status=?, updated_at=?"
            " WHERE kind=? AND action_key=? AND status='executing'",
            (status, datetime.now().isoformat(), kind, key),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("Action claim lost; do not replay")
