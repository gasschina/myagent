"""
记忆系统模块 - 三层记忆架构 + SQLite 持久化
===========================================
- 短期记忆 (Short-Term): 当前对话上下文
- 工作记忆 (Working): 任务进度、步骤、执行历史
- 长期记忆 (Long-Term): 用户偏好、技能经验、历史任务总结
"""
import sqlite3
import json
import uuid
import hashlib
import time
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime
from dataclasses import dataclass, field, asdict
from enum import Enum

from config import get_config


# ============================================================
# 数据模型
# ============================================================

class MemoryType(Enum):
    SHORT_TERM = "short_term"       # 对话消息
    WORKING = "working"             # 任务进度/执行历史
    LONG_TERM = "long_term"         # 长期经验/偏好


class MemoryCategory(Enum):
    """长期记忆细分类别"""
    USER_PREFERENCE = "user_preference"    # 用户偏好
    SKILL_EXPERIENCE = "skill_experience"  # 技能经验
    TASK_SUMMARY = "task_summary"          # 任务总结
    ERROR_LESSON = "error_lesson"          # 错误教训
    GENERAL_KNOWLEDGE = "general"          # 通用知识


@dataclass
class MemoryItem:
    """记忆条目"""
    id: str = ""
    memory_type: str = MemoryType.SHORT_TERM.value
    category: str = ""
    session_id: str = ""
    role: str = ""                # user / assistant / system / tool
    content: str = ""
    summary: str = ""             # 摘要（用于长期记忆）
    metadata: Dict[str, Any] = field(default_factory=dict)
    importance: float = 0.5       # 重要程度 0~1
    access_count: int = 0         # 访问次数
    created_at: float = 0.0
    updated_at: float = 0.0
    expires_at: Optional[float] = None     # 过期时间 (None=永不过期)
    embedding_hash: str = ""      # 内容摘要哈希 (用于去重/相似度)

    def __post_init__(self):
        if not self.id:
            self.id = str(uuid.uuid4())
        if not self.created_at:
            self.created_at = time.time()
        if not self.updated_at:
            self.updated_at = self.created_at
        if self.content and not self.embedding_hash:
            self.embedding_hash = hashlib.md5(
                self.content.encode('utf-8', errors='replace')
            ).hexdigest()


@dataclass
class TaskProgress:
    """任务进度记录"""
    task_id: str = ""
    session_id: str = ""
    description: str = ""
    status: str = "pending"       # pending / running / completed / failed
    plan: str = ""                # 任务计划 JSON
    current_step: int = 0
    total_steps: int = 0
    steps_history: List[Dict] = field(default_factory=list)
    result: str = ""
    error: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0


# ============================================================
# SQLite 存储引擎
# ============================================================

class MemoryStore:
    """SQLite 持久化存储"""

    def __init__(self, db_path: Optional[str] = None):
        cfg = get_config()
        self.db_path = db_path or cfg.get("memory.db_path", "data/myagent.db")
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        """获取线程本地连接"""
        if not hasattr(self._local, 'conn') or self._local.conn is None:
            self._local.conn = sqlite3.connect(
                self.db_path,
                check_same_thread=False,
                timeout=30.0
            )
            self._local.conn.row_factory = sqlite3.Row
            self._local.conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn.execute("PRAGMA foreign_keys=ON")
        return self._local.conn

    def _init_db(self):
        """初始化数据库表"""
        conn = self._get_conn()
        cursor = conn.cursor()

        # 记忆表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                id TEXT PRIMARY KEY,
                memory_type TEXT NOT NULL,
                category TEXT DEFAULT '',
                session_id TEXT DEFAULT '',
                role TEXT DEFAULT '',
                content TEXT NOT NULL,
                summary TEXT DEFAULT '',
                metadata TEXT DEFAULT '{}',
                importance REAL DEFAULT 0.5,
                access_count INTEGER DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                expires_at REAL,
                embedding_hash TEXT DEFAULT ''
            )
        """)

        # 任务进度表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS task_progress (
                task_id TEXT PRIMARY KEY,
                session_id TEXT DEFAULT '',
                description TEXT DEFAULT '',
                status TEXT DEFAULT 'pending',
                plan TEXT DEFAULT '',
                current_step INTEGER DEFAULT 0,
                total_steps INTEGER DEFAULT 0,
                steps_history TEXT DEFAULT '[]',
                result TEXT DEFAULT '',
                error TEXT DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
        """)

        # 会话表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                title TEXT DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                metadata TEXT DEFAULT '{}'
            )
        """)

        # 创建索引
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_memory_type ON memories(memory_type)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_memory_session ON memories(session_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_memory_category ON memories(category)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_memory_hash ON memories(embedding_hash)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_memory_created ON memories(created_at)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_memory_importance ON memories(importance)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_task_session ON task_progress(session_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_task_status ON task_progress(status)")

        conn.commit()

    # --------------------------------------------------------
    # 记忆 CRUD
    # --------------------------------------------------------

    def add_memory(self, item: MemoryItem) -> str:
        """添加一条记忆"""
        conn = self._get_conn()
        conn.execute("""
            INSERT OR REPLACE INTO memories
            (id, memory_type, category, session_id, role, content, summary,
             metadata, importance, access_count, created_at, updated_at,
             expires_at, embedding_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            item.id, item.memory_type, item.category, item.session_id,
            item.role, item.content, item.summary,
            json.dumps(item.metadata, ensure_ascii=False),
            item.importance, item.access_count,
            item.created_at, item.updated_at,
            item.expires_at, item.embedding_hash
        ))
        conn.commit()
        return item.id

    def get_memory(self, memory_id: str) -> Optional[MemoryItem]:
        """获取单条记忆"""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM memories WHERE id = ?", (memory_id,)
        ).fetchone()
        if row:
            return self._row_to_item(row)
        return None

    def update_memory(self, memory_id: str, updates: Dict[str, Any]) -> bool:
        """更新记忆"""
        allowed = {'content', 'summary', 'metadata', 'importance', 'category', 'expires_at'}
        set_parts = []
        values = []
        for key in allowed:
            if key in updates:
                set_parts.append(f"{key} = ?")
                if key == 'metadata':
                    values.append(json.dumps(updates[key], ensure_ascii=False))
                else:
                    values.append(updates[key])

        if not set_parts:
            return False

        set_parts.append("updated_at = ?")
        values.append(time.time())
        values.append(memory_id)

        conn = self._get_conn()
        cursor = conn.execute(
            f"UPDATE memories SET {', '.join(set_parts)} WHERE id = ?",
            values
        )
        conn.commit()
        return cursor.rowcount > 0

    def delete_memory(self, memory_id: str) -> bool:
        """删除记忆"""
        conn = self._get_conn()
        cursor = conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
        conn.commit()
        return cursor.rowcount > 0

    def touch_memory(self, memory_id: str) -> None:
        """增加访问计数"""
        conn = self._get_conn()
        conn.execute("""
            UPDATE memories SET access_count = access_count + 1, updated_at = ?
            WHERE id = ?
        """, (time.time(), memory_id))
        conn.commit()

    def _row_to_item(self, row: sqlite3.Row) -> MemoryItem:
        """Row -> MemoryItem"""
        return MemoryItem(
            id=row['id'],
            memory_type=row['memory_type'],
            category=row['category'],
            session_id=row['session_id'],
            role=row['role'],
            content=row['content'],
            summary=row['summary'],
            metadata=json.loads(row['metadata']),
            importance=row['importance'],
            access_count=row['access_count'],
            created_at=row['created_at'],
            updated_at=row['updated_at'],
            expires_at=row['expires_at'],
            embedding_hash=row['embedding_hash'],
        )

    # --------------------------------------------------------
    # 查询接口
    # --------------------------------------------------------

    def get_session_messages(
        self,
        session_id: str,
        limit: int = 50,
        offset: int = 0
    ) -> List[MemoryItem]:
        """获取会话的短期记忆（对话消息）"""
        conn = self._get_conn()
        rows = conn.execute("""
            SELECT * FROM memories
            WHERE session_id = ? AND memory_type = ?
            ORDER BY created_at ASC
            LIMIT ? OFFSET ?
        """, (session_id, MemoryType.SHORT_TERM.value, limit, offset)).fetchall()
        return [self._row_to_item(r) for r in rows]

    def get_working_memories(
        self,
        session_id: Optional[str] = None,
        limit: int = 200
    ) -> List[MemoryItem]:
        """获取工作记忆"""
        conn = self._get_conn()
        if session_id:
            rows = conn.execute("""
                SELECT * FROM memories
                WHERE memory_type = ? AND session_id = ?
                ORDER BY created_at DESC
                LIMIT ?
            """, (MemoryType.WORKING.value, session_id, limit)).fetchall()
        else:
            rows = conn.execute("""
                SELECT * FROM memories
                WHERE memory_type = ?
                ORDER BY created_at DESC
                LIMIT ?
            """, (MemoryType.WORKING.value, limit)).fetchall()
        return [self._row_to_item(r) for r in rows]

    def search_long_term(
        self,
        query: str,
        category: Optional[str] = None,
        limit: int = 20,
        min_importance: float = 0.3
    ) -> List[MemoryItem]:
        """
        搜索长期记忆
        使用关键词匹配 + 重要度排序
        """
        conn = self._get_conn()
        query_lower = query.lower()
        # SQLite FTS5 如果需要可以扩展
        conditions = [
            "memory_type = ?",
            "importance >= ?",
        ]
        params: list = [MemoryType.LONG_TERM.value, min_importance]

        if category:
            conditions.append("category = ?")
            params.append(category)

        # 关键词模糊匹配 (content LIKE)
        keywords = query_lower.split()
        keyword_conditions = []
        for kw in keywords:
            if len(kw) >= 2:  # 忽略太短的关键词
                keyword_conditions.append("LOWER(content) LIKE ?")
                params.append(f"%{kw}%")

        if keyword_conditions:
            conditions.append(f"({' OR '.join(keyword_conditions)})")

        where = " AND ".join(conditions)
        rows = conn.execute(f"""
            SELECT * FROM memories
            WHERE {where}
            ORDER BY importance DESC, access_count DESC, updated_at DESC
            LIMIT ?
        """, params + [limit]).fetchall()

        return [self._row_to_item(r) for r in rows]

    def find_similar_memories(
        self,
        content: str,
        memory_type: str = MemoryType.LONG_TERM.value,
        limit: int = 10
    ) -> List[MemoryItem]:
        """查找相似记忆（基于内容哈希去重 + 关键词匹配）"""
        conn = self._get_conn()
        content_hash = hashlib.md5(
            content.encode('utf-8', errors='replace')
        ).hexdigest()

        # 先尝试精确匹配哈希
        exact = conn.execute("""
            SELECT * FROM memories
            WHERE embedding_hash = ? AND memory_type = ?
            ORDER BY updated_at DESC LIMIT ?
        """, (content_hash, memory_type, limit)).fetchall()

        if exact:
            return [self._row_to_item(r) for r in exact]

        # 关键词匹配
        keywords = content.lower().split()[:10]  # 最多取前10个关键词
        if not keywords:
            return []

        conditions = []
        params = []
        for kw in keywords:
            if len(kw) >= 2:
                conditions.append("LOWER(content) LIKE ?")
                params.append(f"%{kw}%")

        if not conditions:
            return []

        where = f"({ ' OR '.join(conditions) }) AND memory_type = ?"
        rows = conn.execute(f"""
            SELECT * FROM memories WHERE {where}
            ORDER BY importance DESC LIMIT ?
        """, params + [memory_type, limit]).fetchall()

        return [self._row_to_item(r) for r in rows]

    def cleanup_expired(self) -> int:
        """清理过期记忆"""
        conn = self._get_conn()
        now = time.time()
        cursor = conn.execute(
            "DELETE FROM memories WHERE expires_at IS NOT NULL AND expires_at < ?",
            (now,)
        )
        conn.commit()
        return cursor.rowcount

    def cleanup_session(self, session_id: str, keep_types: Optional[List[str]] = None) -> int:
        """清理指定会话的记忆"""
        conn = self._get_conn()
        if keep_types:
            placeholders = ','.join(['?'] * len(keep_types))
            cursor = conn.execute(
                f"DELETE FROM memories WHERE session_id = ? AND memory_type NOT IN ({placeholders})",
                [session_id] + keep_types
            )
        else:
            cursor = conn.execute(
                "DELETE FROM memories WHERE session_id = ?", (session_id,)
            )
        conn.commit()
        return cursor.rowcount

    def get_memory_stats(self) -> Dict[str, int]:
        """获取记忆统计"""
        conn = self._get_conn()
        stats = {}
        for mt in MemoryType:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM memories WHERE memory_type = ?",
                (mt.value,)
            ).fetchone()
            stats[mt.value] = row['cnt']
        total = conn.execute("SELECT COUNT(*) as cnt FROM memories").fetchone()
        stats['total'] = total['cnt']
        sessions = conn.execute("SELECT COUNT(*) as cnt FROM sessions").fetchone()
        stats['sessions'] = sessions['cnt']
        return stats

    # --------------------------------------------------------
    # 任务进度 CRUD
    # --------------------------------------------------------

    def save_task_progress(self, task: TaskProgress) -> str:
        """保存/更新任务进度"""
        conn = self._get_conn()
        conn.execute("""
            INSERT OR REPLACE INTO task_progress
            (task_id, session_id, description, status, plan,
             current_step, total_steps, steps_history, result, error,
             created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            task.task_id, task.session_id, task.description, task.status,
            task.plan, task.current_step, task.total_steps,
            json.dumps(task.steps_history, ensure_ascii=False),
            task.result, task.error, task.created_at, task.updated_at
        ))
        conn.commit()
        return task.task_id

    def get_task_progress(self, task_id: str) -> Optional[TaskProgress]:
        """获取任务进度"""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM task_progress WHERE task_id = ?", (task_id,)
        ).fetchone()
        if row:
            return TaskProgress(
                task_id=row['task_id'],
                session_id=row['session_id'],
                description=row['description'],
                status=row['status'],
                plan=row['plan'],
                current_step=row['current_step'],
                total_steps=row['total_steps'],
                steps_history=json.loads(row['steps_history']),
                result=row['result'],
                error=row['error'],
                created_at=row['created_at'],
                updated_at=row['updated_at'],
            )
        return None

    def get_active_tasks(self, session_id: Optional[str] = None) -> List[TaskProgress]:
        """获取活跃任务"""
        conn = self._get_conn()
        if session_id:
            rows = conn.execute("""
                SELECT * FROM task_progress
                WHERE session_id = ? AND status IN ('pending', 'running')
                ORDER BY created_at DESC
            """, (session_id,)).fetchall()
        else:
            rows = conn.execute("""
                SELECT * FROM task_progress
                WHERE status IN ('pending', 'running')
                ORDER BY created_at DESC
            """).fetchall()

        return [
            TaskProgress(
                task_id=r['task_id'],
                session_id=r['session_id'],
                description=r['description'],
                status=r['status'],
                plan=r['plan'],
                current_step=r['current_step'],
                total_steps=r['total_steps'],
                steps_history=json.loads(r['steps_history']),
                result=r['result'],
                error=r['error'],
                created_at=r['created_at'],
                updated_at=r['updated_at'],
            )
            for r in rows
        ]

    # --------------------------------------------------------
    # 会话管理
    # --------------------------------------------------------

    def create_session(self, session_id: Optional[str] = None, title: str = "") -> str:
        """创建新会话"""
        sid = session_id or str(uuid.uuid4())
        now = time.time()
        conn = self._get_conn()
        conn.execute("""
            INSERT OR IGNORE INTO sessions (session_id, title, created_at, updated_at, metadata)
            VALUES (?, ?, ?, ?, '{}')
        """, (sid, title, now, now))
        conn.commit()
        return sid

    def get_sessions(self, limit: int = 50) -> List[Dict]:
        """获取会话列表"""
        conn = self._get_conn()
        rows = conn.execute("""
            SELECT s.*, COUNT(m.id) as message_count
            FROM sessions s
            LEFT JOIN memories m ON s.session_id = m.session_id
            GROUP BY s.session_id
            ORDER BY s.updated_at DESC
            LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]


# ============================================================
# 记忆管理器 (高层接口)
# ============================================================

class MemoryManager:
    """
    记忆管理器 - 统一管理三层记忆
    提供给 Agent 使用的简洁 API
    """

    def __init__(self, store: Optional[MemoryStore] = None):
        cfg = get_config()
        self.store = store or MemoryStore()
        self.max_short_term = cfg.get("memory.max_short_term_messages", 50)
        self.max_working = cfg.get("memory.max_working_memory_items", 200)
        self.auto_summarize_threshold = cfg.get("memory.auto_summarize_threshold", 40)
        self._lock = threading.Lock()

    # --------------------------------------------------------
    # 短期记忆
    # --------------------------------------------------------

    def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        metadata: Optional[Dict] = None
    ) -> str:
        """添加对话消息到短期记忆"""
        with self._lock:
            # 检查是否需要摘要压缩
            msgs = self.store.get_session_messages(session_id, limit=self.max_short_term)
            if len(msgs) >= self.max_short_term:
                self._auto_summarize(session_id, msgs)

            item = MemoryItem(
                memory_type=MemoryType.SHORT_TERM.value,
                session_id=session_id,
                role=role,
                content=content,
                metadata=metadata or {},
                importance=0.3,
            )
            return self.store.add_memory(item)

    def get_conversation(
        self,
        session_id: str,
        limit: Optional[int] = None,
        as_dicts: bool = True
    ) -> List[Dict]:
        """获取会话对话历史"""
        n = limit or self.max_short_term
        items = self.store.get_session_messages(session_id, limit=n)
        if as_dicts:
            return [
                {"role": m.role, "content": m.content}
                for m in items
            ]
        return items

    def _auto_summarize(self, session_id: str, messages: List[MemoryItem]) -> None:
        """自动摘要：将旧消息压缩为工作记忆"""
        # 保留最近的消息，压缩旧消息
        keep_count = self.max_short_term // 2
        old_messages = messages[:-keep_count]

        if len(old_messages) < 5:
            return

        # 生成摘要
        summary_parts = []
        for m in old_messages:
            if m.role in ('user', 'assistant'):
                prefix = "用户" if m.role == 'user' else "助手"
                content = m.content[:200] + ("..." if len(m.content) > 200 else "")
                summary_parts.append(f"{prefix}: {content}")

        summary_text = "\n".join(summary_parts[:20])
        if len(summary_parts) > 20:
            summary_text += f"\n... (共{len(summary_parts)}条消息已压缩)"

        # 存为工作记忆
        item = MemoryItem(
            memory_type=MemoryType.WORKING.value,
            category="conversation_summary",
            session_id=session_id,
            content=summary_text,
            summary=f"会话摘要: {len(old_messages)}条消息",
            importance=0.4,
        )
        self.store.add_memory(item)

        # 删除旧消息
        for m in old_messages:
            self.store.delete_memory(m.id)

    # --------------------------------------------------------
    # 工作记忆
    # --------------------------------------------------------

    def add_working_memory(
        self,
        session_id: str,
        content: str,
        category: str = "",
        metadata: Optional[Dict] = None,
        importance: float = 0.5
    ) -> str:
        """添加工作记忆"""
        item = MemoryItem(
            memory_type=MemoryType.WORKING.value,
            category=category,
            session_id=session_id,
            content=content,
            metadata=metadata or {},
            importance=importance,
        )
        return self.store.add_memory(item)

    def get_working_context(self, session_id: str) -> str:
        """获取工作上下文（给LLM看）"""
        items = self.store.get_working_memories(session_id, limit=20)
        if not items:
            return ""

        parts = ["=== 工作记忆 ==="]
        for item in items[:10]:
            prefix = ""
            if item.category:
                prefix = f"[{item.category}] "
            parts.append(f"{prefix}{item.content[:500]}")
        return "\n".join(parts)

    def record_execution(
        self,
        session_id: str,
        tool_name: str,
        input_data: str,
        output_data: str,
        success: bool = True,
        error: str = ""
    ) -> str:
        """记录一次执行到工作记忆"""
        status = "成功" if success else "失败"
        content = f"执行 {tool_name} {status}"
        if error:
            content += f"\n错误: {error[:500]}"
        content += f"\n输入: {input_data[:300]}"
        content += f"\n输出: {output_data[:500]}"

        return self.add_working_memory(
            session_id=session_id,
            content=content,
            category="execution",
            importance=0.6 if success else 0.8,
            metadata={
                "tool": tool_name,
                "success": success,
                "error": error[:200] if error else "",
            }
        )

    # --------------------------------------------------------
    # 长期记忆
    # --------------------------------------------------------

    def learn(
        self,
        content: str,
        category: str = MemoryCategory.GENERAL_KNOWLEDGE.value,
        summary: str = "",
        importance: float = 0.5,
        session_id: str = "",
        metadata: Optional[Dict] = None
    ) -> str:
        """写入长期记忆"""
        # 去重检查
        similar = self.store.find_similar_memories(content, limit=3)
        for s in similar:
            if s.embedding_hash == MemoryItem(content=content).embedding_hash:
                # 内容相同，更新而非新建
                if importance > s.importance:
                    self.store.update_memory(s.id, {
                        "importance": importance,
                        "summary": summary,
                    })
                    return s.id
                return s.id

        item = MemoryItem(
            memory_type=MemoryType.LONG_TERM.value,
            category=category,
            content=content,
            summary=summary,
            importance=importance,
            session_id=session_id,
            metadata=metadata or {},
        )
        return self.store.add_memory(item)

    def learn_from_error(
        self,
        error_description: str,
        error_type: str = "",
        fix: str = "",
        context: str = ""
    ) -> str:
        """从错误中学习"""
        content = f"错误类型: {error_type}\n错误描述: {error_description}\n修复方法: {fix}\n上下文: {context}"
        summary = f"错误教训: {error_type} - {error_description[:100]}"
        return self.learn(
            content=content,
            category=MemoryCategory.ERROR_LESSON.value,
            summary=summary,
            importance=0.8,  # 错误教训更重要
        )

    def learn_preference(self, key: str, value: str) -> str:
        """学习用户偏好"""
        content = f"偏好 {key}: {value}"
        return self.learn(
            content=content,
            category=MemoryCategory.USER_PREFERENCE.value,
            summary=f"用户偏好: {key}={value[:50]}",
            importance=0.7,
        )

    def recall(
        self,
        query: str,
        category: Optional[str] = None,
        limit: int = 10
    ) -> List[MemoryItem]:
        """检索长期记忆"""
        results = self.store.search_long_term(query, category=category, limit=limit)
        # 触摸访问计数
        for r in results:
            self.store.touch_memory(r.id)
        return results

    def recall_errors(self, error_query: str, limit: int = 5) -> List[MemoryItem]:
        """检索过去的错误教训"""
        return self.recall(
            query=error_query,
            category=MemoryCategory.ERROR_LESSON.value,
            limit=limit
        )

    def get_context_for_llm(self, session_id: str, query: str = "") -> str:
        """
        获取完整的记忆上下文供 LLM 使用
        包含: 相关长期记忆 + 工作记忆
        """
        parts = []

        # 检索相关长期记忆
        if query:
            relevant = self.recall(query, limit=5)
            if relevant:
                parts.append("=== 相关经验 ===")
                for item in relevant:
                    label = item.category or "记忆"
                    parts.append(f"[{label}] {item.content[:300]}")

        # 工作记忆
        working_ctx = self.get_working_context(session_id)
        if working_ctx:
            parts.append(working_ctx)

        return "\n\n".join(parts)

    # --------------------------------------------------------
    # 任务进度
    # --------------------------------------------------------

    def create_task(
        self,
        session_id: str,
        description: str,
        plan: str = ""
    ) -> str:
        """创建新任务"""
        task = TaskProgress(
            task_id=str(uuid.uuid4()),
            session_id=session_id,
            description=description,
            status="running",
            plan=plan,
            created_at=time.time(),
            updated_at=time.time(),
        )
        self.store.save_task_progress(task)
        return task.task_id

    def update_task(
        self,
        task_id: str,
        status: Optional[str] = None,
        current_step: Optional[int] = None,
        total_steps: Optional[int] = None,
        step_record: Optional[Dict] = None,
        result: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        """更新任务进度"""
        task = self.store.get_task_progress(task_id)
        if not task:
            return

        if status:
            task.status = status
        if current_step is not None:
            task.current_step = current_step
        if total_steps is not None:
            task.total_steps = total_steps
        if step_record:
            task.steps_history.append(step_record)
        if result:
            task.result = result
        if error:
            task.error = error
        task.updated_at = time.time()

        self.store.save_task_progress(task)

    def complete_task(self, task_id: str, result: str = "") -> None:
        """完成任务"""
        self.update_task(task_id, status="completed", result=result)

    def fail_task(self, task_id: str, error: str = "") -> None:
        """标记任务失败"""
        self.update_task(task_id, status="failed", error=error)

    def get_task(self, task_id: str) -> Optional[TaskProgress]:
        return self.store.get_task_progress(task_id)

    # --------------------------------------------------------
    # 维护
    # --------------------------------------------------------

    def maintenance(self) -> Dict[str, Any]:
        """执行记忆维护"""
        expired = self.store.cleanup_expired()
        stats = self.store.get_memory_stats()
        return {
            "expired_cleaned": expired,
            "stats": stats,
        }

    def export_session(self, session_id: str) -> Dict:
        """导出会话数据"""
        messages = self.store.get_session_messages(session_id, limit=9999)
        working = self.store.get_working_memories(session_id, limit=9999)
        return {
            "session_id": session_id,
            "messages": [asdict(m) for m in messages],
            "working_memories": [asdict(m) for m in working],
        }
