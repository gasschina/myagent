"""
memory/manager.py - 记忆管理器
================================
基于 SQLite 的三层记忆系统:
  - 短期记忆 (short_term): 当前对话上下文，自动淘汰旧消息
  - 工作记忆 (working): 任务进度、执行步骤、中间结果
  - 长期记忆 (long_term): 用户偏好、技能经验、历史任务总结

所有记忆通过 session_id 隔离，支持跨会话检索。
"""
from __future__ import annotations

import json
import sqlite3
import time
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from core.logger import get_logger
from core.utils import generate_id, timestamp, truncate_str

logger = get_logger("myagent.memory")


# ==============================================================================
# 数据模型
# ==============================================================================

@dataclass
class MemoryEntry:
    """记忆条目"""
    id: str = field(default_factory=lambda: generate_id("mem"))
    session_id: str = "default"
    category: str = "short_term"     # short_term | working | long_term
    key: str = ""                    # 检索键/标签
    content: str = ""                # 记忆内容
    summary: str = ""                # 摘要(长期记忆用)
    role: str = ""                   # 对话角色: user | assistant | system | tool
    metadata: Dict[str, Any] = field(default_factory=dict)
    importance: float = 0.5          # 重要性 0~1
    access_count: int = 0            # 访问次数
    created_at: str = field(default_factory=timestamp)
    updated_at: str = field(default_factory=timestamp)
    expires_at: str = ""             # 过期时间(空=永不过期)

    def to_dict(self) -> dict:
        d = asdict(self)
        # metadata 序列化
        d["metadata"] = json.dumps(self.metadata, ensure_ascii=False)
        return d

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "MemoryEntry":
        meta = row["metadata"]
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except json.JSONDecodeError:
                meta = {}
        return cls(
            id=row["id"],
            session_id=row["session_id"],
            category=row["category"],
            key=row["key"],
            content=row["content"],
            summary=row["summary"],
            role=row["role"],
            metadata=meta,
            importance=row["importance"],
            access_count=row["access_count"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            expires_at=dict(row).get("expires_at", ""),
        )


# ==============================================================================
# 记忆管理器
# ==============================================================================

class MemoryManager:
    """
    三层记忆管理器。

    使用示例:
        mm = MemoryManager(db_path="~/.myagent/data/memory.db")
        mm.initialize()

        # 写入短期记忆
        mm.add_short_term("session_1", "user", "帮我创建一个Python项目")
        mm.add_short_term("session_1", "assistant", "好的，正在创建...")

        # 查询对话历史
        history = mm.get_conversation("session_1")

        # 写入工作记忆
        mm.add_working("session_1", "task_progress", "步骤2/5: 已创建目录结构")

        # 写入长期记忆
        mm.add_long_term("user_prefs", "coding_style", "偏好使用Python, TypeScript")

        # 语义搜索
        results = mm.search("Python项目", category="long_term")
    """

    def __init__(self, db_path: str = ""):
        self.db_path = db_path
        self._local = threading.local()
        self._initialized = False

    def _get_conn(self) -> sqlite3.Connection:
        """获取线程本地的数据库连接"""
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = sqlite3.connect(
                self.db_path,
                check_same_thread=False,
                timeout=10,
            )
            self._local.conn.row_factory = sqlite3.Row
            self._local.conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn.execute("PRAGMA synchronous=NORMAL")
        return self._local.conn

    def initialize(self):
        """初始化数据库表结构"""
        conn = self._get_conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS memories (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                category TEXT NOT NULL,
                key TEXT DEFAULT '',
                content TEXT DEFAULT '',
                summary TEXT DEFAULT '',
                role TEXT DEFAULT '',
                metadata TEXT DEFAULT '{}',
                importance REAL DEFAULT 0.5,
                access_count INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                expires_at TEXT DEFAULT ''
            );

            CREATE INDEX IF NOT EXISTS idx_session ON memories(session_id);
            CREATE INDEX IF NOT EXISTS idx_category ON memories(category);
            CREATE INDEX IF NOT EXISTS idx_key ON memories(key);
            CREATE INDEX IF NOT EXISTS idx_session_category ON memories(session_id, category);
            CREATE INDEX IF NOT EXISTS idx_importance ON memories(importance DESC);
        """)
        conn.commit()
        self._initialized = True
        logger.info(f"记忆系统已初始化 (db={self.db_path})")

    def close(self):
        """关闭数据库连接"""
        if hasattr(self._local, "conn") and self._local.conn:
            self._local.conn.close()
            self._local.conn = None

    # ==========================================================================
    # 基础 CRUD
    # ==========================================================================

    def _insert(self, entry: MemoryEntry) -> str:
        """插入记忆条目"""
        conn = self._get_conn()
        entry.updated_at = timestamp()
        try:
            conn.execute(
                """INSERT OR REPLACE INTO memories
                   (id, session_id, category, key, content, summary, role,
                    metadata, importance, access_count, created_at, updated_at, expires_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    entry.id, entry.session_id, entry.category, entry.key,
                    entry.content, entry.summary, entry.role,
                    json.dumps(entry.metadata, ensure_ascii=False),
                    entry.importance, entry.access_count,
                    entry.created_at, entry.updated_at, entry.expires_at,
                ),
            )
            conn.commit()
            return entry.id
        except Exception as e:
            logger.error(f"记忆写入失败: {e}")
            raise

    def _query(
        self,
        session_id: str = "",
        category: str = "",
        key: str = "",
        role: str = "",
        limit: int = 100,
        order_by: str = "created_at ASC",
    ) -> List[MemoryEntry]:
        """查询记忆条目"""
        conn = self._get_conn()
        conditions = []
        params: list = []

        if session_id:
            conditions.append("session_id = ?")
            params.append(session_id)
        if category:
            conditions.append("category = ?")
            params.append(category)
        if key:
            conditions.append("key = ?")
            params.append(key)
        if role:
            conditions.append("role = ?")
            params.append(role)

        # 过滤已过期的
        conditions.append("(expires_at = '' OR expires_at > ?)")
        params.append(timestamp())

        where = " AND ".join(conditions) if conditions else "1=1"
        sql = f"SELECT * FROM memories WHERE {where} ORDER BY {order_by} LIMIT ?"
        params.append(limit)

        rows = conn.execute(sql, params).fetchall()
        return [MemoryEntry.from_row(row) for row in rows]

    def _delete(self, session_id: str, category: str = "", older_than: str = "") -> int:
        """删除记忆条目，返回删除数量"""
        conn = self._get_conn()
        conditions = ["session_id = ?"]
        params: list = [session_id]

        if category:
            conditions.append("category = ?")
            params.append(category)
        if older_than:
            conditions.append("created_at < ?")
            params.append(older_than)

        where = " AND ".join(conditions)
        cursor = conn.execute(f"DELETE FROM memories WHERE {where}", params)
        conn.commit()
        return cursor.rowcount

    def _update_content(self, memory_id: str, content: str, **updates):
        """更新记忆内容"""
        conn = self._get_conn()
        sets = ["content = ?", "updated_at = ?"]
        params: list = [content, timestamp()]

        for key, val in updates.items():
            if key in ("summary", "key", "importance", "metadata", "access_count", "expires_at"):
                sets.append(f"{key} = ?")
                if key == "metadata":
                    params.append(json.dumps(val, ensure_ascii=False))
                else:
                    params.append(val)

        params.append(memory_id)
        conn.execute(f"UPDATE memories SET {', '.join(sets)} WHERE id = ?", params)
        conn.commit()

    # ==========================================================================
    # 短期记忆 (对话上下文)
    # ==========================================================================

    def add_short_term(
        self,
        session_id: str,
        role: str,
        content: str,
        key: str = "",
        importance: float = 0.5,
    ) -> str:
        """
        添加短期记忆(对话消息)。

        Args:
            session_id: 会话ID
            role: 角色 (user/assistant/system/tool)
            content: 消息内容
            key: 检索标签
            importance: 重要性权重
        """
        entry = MemoryEntry(
            session_id=session_id,
            category="short_term",
            role=role,
            content=truncate_str(content, 50000),
            key=key,
            importance=importance,
        )
        return self._insert(entry)

    def get_conversation(
        self,
        session_id: str,
        limit: int = 50,
        include_roles: Optional[List[str]] = None,
    ) -> List[MemoryEntry]:
        """获取对话历史"""
        entries = self._query(
            session_id=session_id,
            category="short_term",
            limit=limit,
            order_by="created_at ASC",
        )
        if include_roles:
            entries = [e for e in entries if e.role in include_roles]
        return entries

    def get_conversation_text(
        self,
        session_id: str,
        limit: int = 50,
    ) -> str:
        """获取对话历史文本(供 LLM 使用)"""
        entries = self.get_conversation(session_id, limit)
        lines = []
        for e in entries:
            label = e.role.upper()
            if e.role == "user":
                label = "用户"
            elif e.role == "assistant":
                label = "助手"
            elif e.role == "system":
                label = "系统"
            elif e.role == "tool":
                label = "工具"
            lines.append(f"[{label}] {e.content}")
        return "\n".join(lines)

    def clear_conversation(self, session_id: str) -> int:
        """清空会话对话历史"""
        return self._delete(session_id, category="short_term")

    def prune_conversation(self, session_id: str, max_messages: int = 50) -> int:
        """修剪对话历史，保留最近 N 条"""
        entries = self.get_conversation(session_id, limit=1000)
        if len(entries) <= max_messages:
            return 0
        # 删除最旧的
        to_remove = entries[:-max_messages]
        conn = self._get_conn()
        ids = [e.id for e in to_remove]
        placeholders = ",".join("?" * len(ids))
        cursor = conn.execute(f"DELETE FROM memories WHERE id IN ({placeholders})", ids)
        conn.commit()
        logger.debug(f"修剪对话历史: 删除 {len(ids)} 条 (session={session_id})")
        return len(ids)

    # ==========================================================================
    # 工作记忆 (任务进度)
    # ==========================================================================

    def add_working(
        self,
        session_id: str,
        key: str,
        content: str,
        metadata: Optional[Dict] = None,
    ) -> str:
        """
        添加工作记忆(任务进度)。

        Args:
            session_id: 会话ID
            key: 进度键(如 "task_progress", "step_result", "error_log")
            content: 进度内容
            metadata: 附加数据
        """
        entry = MemoryEntry(
            session_id=session_id,
            category="working",
            key=key,
            content=truncate_str(content, 30000),
            metadata=metadata or {},
            importance=0.7,
        )
        return self._insert(entry)

    def get_working(
        self,
        session_id: str,
        key: str = "",
    ) -> List[MemoryEntry]:
        """获取工作记忆"""
        return self._query(
            session_id=session_id,
            category="working",
            key=key,
            order_by="created_at DESC",
        )

    def update_working(self, memory_id: str, content: str, **updates):
        """更新工作记忆"""
        self._update_content(memory_id, content, **updates)

    def clear_working(self, session_id: str) -> int:
        """清空工作记忆"""
        return self._delete(session_id, category="working")

    # ==========================================================================
    # 长期记忆 (持久知识)
    # ==========================================================================

    def add_long_term(
        self,
        session_id: str = "global",
        key: str = "",
        content: str = "",
        summary: str = "",
        importance: float = 0.7,
        metadata: Optional[Dict] = None,
    ) -> str:
        """
        添加长期记忆。

        Args:
            session_id: 全局使用 "global"
            key: 知识分类(如 "user_prefs", "skill_experience", "task_summary")
            content: 详细内容
            summary: 简要摘要
            importance: 重要性(越高越不容易被淘汰)
        """
        entry = MemoryEntry(
            session_id=session_id,
            category="long_term",
            key=key,
            content=truncate_str(content, 50000),
            summary=summary,
            importance=importance,
            metadata=metadata or {},
        )
        return self._insert(entry)

    def get_long_term(
        self,
        session_id: str = "global",
        key: str = "",
        limit: int = 50,
    ) -> List[MemoryEntry]:
        """获取长期记忆"""
        return self._query(
            session_id=session_id,
            category="long_term",
            key=key,
            limit=limit,
            order_by="importance DESC, created_at DESC",
        )

    def get_preferences(self, session_id: str = "global") -> List[MemoryEntry]:
        """获取用户偏好"""
        return self.get_long_term(session_id, key="user_pref")

    def get_experience(self, session_id: str = "global") -> List[MemoryEntry]:
        """获取技能经验"""
        return self.get_long_term(session_id, key="skill_experience")

    def get_task_summaries(self, session_id: str = "global") -> List[MemoryEntry]:
        """获取历史任务总结"""
        return self.get_long_term(session_id, key="task_summary")

    # ==========================================================================
    # 记忆搜索
    # ==========================================================================

    def search(
        self,
        query: str,
        session_id: str = "",
        category: str = "",
        limit: int = 10,
    ) -> List[MemoryEntry]:
        """
        关键词搜索记忆。

        在 content, summary, key, metadata 中搜索匹配项。
        """
        conn = self._get_conn()
        conditions = ["1=1"]
        params: list = []

        if session_id:
            conditions.append("session_id = ?")
            params.append(session_id)
        if category:
            conditions.append("category = ?")
            params.append(category)

        # 过滤过期
        conditions.append("(expires_at = '' OR expires_at > ?)")
        params.append(timestamp())

        # 关键词搜索
        like_pattern = f"%{query}%"
        conditions.append(
            "(content LIKE ? OR summary LIKE ? OR key LIKE ? OR metadata LIKE ?)"
        )
        params.extend([like_pattern, like_pattern, like_pattern, like_pattern])

        where = " AND ".join(conditions)
        sql = f"""
            SELECT * FROM memories WHERE {where}
            ORDER BY importance DESC, access_count DESC
            LIMIT ?
        """
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()

        # 更新访问计数
        for row in rows:
            conn.execute(
                "UPDATE memories SET access_count = access_count + 1 WHERE id = ?",
                (row["id"],),
            )
        conn.commit()

        return [MemoryEntry.from_row(row) for row in rows]

    def search_across_sessions(
        self,
        query: str,
        category: str = "",
        limit: int = 20,
    ) -> List[MemoryEntry]:
        """跨会话搜索"""
        return self.search(query, session_id="", category=category, limit=limit)

    # ==========================================================================
    # 记忆总结与维护
    # ==========================================================================

    def get_recent_for_summary(self, session_id: str, count: int = 20) -> List[MemoryEntry]:
        """获取最近的对话用于总结"""
        return self.get_conversation(session_id, limit=count)

    def save_summary(
        self,
        session_id: str,
        summary: str,
        original_count: int = 0,
    ) -> str:
        """保存对话总结为长期记忆"""
        return self.add_long_term(
            session_id=session_id,
            key="conversation_summary",
            content=summary,
            summary=summary[:500],
            importance=0.6,
            metadata={"original_message_count": original_count},
        )

    def get_error_patterns(self, session_id: str = "global", limit: int = 20) -> List[MemoryEntry]:
        """获取历史错误模式(用于避免重复犯错)"""
        return self._query(
            session_id=session_id,
            category="long_term",
            key="error_pattern",
            limit=limit,
            order_by="importance DESC",
        )

    def record_error_pattern(
        self,
        error: str,
        fix: str = "",
        session_id: str = "global",
    ):
        """记录错误模式及修复方案"""
        self.add_long_term(
            session_id=session_id,
            key="error_pattern",
            content=f"错误: {error}\n修复: {fix}",
            summary=f"错误: {truncate_str(error, 200)} | 修复: {truncate_str(fix, 200)}",
            importance=0.8,
        )

    # ==========================================================================
    # 统计与管理
    # ==========================================================================

    def get_stats(self) -> Dict[str, Any]:
        """获取记忆系统统计"""
        conn = self._get_conn()
        stats = {}
        for cat in ("short_term", "working", "long_term"):
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM memories WHERE category = ?", (cat,)
            ).fetchone()
            stats[f"{cat}_count"] = row["cnt"]

        total = conn.execute("SELECT COUNT(*) as cnt FROM memories").fetchone()
        stats["total_count"] = total["cnt"]

        sessions = conn.execute(
            "SELECT COUNT(DISTINCT session_id) as cnt FROM memories"
        ).fetchone()
        stats["session_count"] = sessions["cnt"]

        return stats

    def cleanup_expired(self) -> int:
        """清理过期记忆"""
        conn = self._get_conn()
        now = timestamp()
        cursor = conn.execute(
            "DELETE FROM memories WHERE expires_at != '' AND expires_at < ?",
            (now,),
        )
        conn.commit()
        count = cursor.rowcount
        if count > 0:
            logger.info(f"清理过期记忆: {count} 条")
        return count

    def export_session(self, session_id: str) -> Dict[str, Any]:
        """导出会话所有记忆"""
        entries = self._query(session_id=session_id, limit=10000)
        return {
            "session_id": session_id,
            "exported_at": timestamp(),
            "memories": [asdict(e) for e in entries],
        }
