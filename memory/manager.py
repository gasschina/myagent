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
import math
import re
import sqlite3
import time
import threading
from collections import Counter, defaultdict
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

    # ==========================================================================
    # TF-IDF 语义搜索 (无外部依赖)
    # ==========================================================================

    @staticmethod
    def _tokenize(text: str) -> List[str]:
        """
        中文分词（简单实现：单字+双字组合）+ 英文词提取。

        对中文文本提取单字和相邻双字作为 token，
        对英文文本按空格和标点分词并转小写。
        """
        if not text:
            return []

        tokens: List[str] = []
        # 提取中文连续片段
        chinese_segments = re.findall(r'[\u4e00-\u9fff]+', text)
        for seg in chinese_segments:
            # 单字
            tokens.extend(list(seg))
            # 双字组合
            for i in range(len(seg) - 1):
                tokens.append(seg[i:i + 2])

        # 提取英文/数字单词
        english_words = re.findall(r'[a-zA-Z0-9]+', text.lower())
        tokens.extend(english_words)

        return tokens

    @staticmethod
    def _compute_tf(tokens: List[str]) -> Counter:
        """计算词频 (Term Frequency)"""
        return Counter(tokens)

    @classmethod
    def _compute_tfidf(
        cls,
        query: str,
        documents: List[Tuple[str, str]],  # [(id, text), ...]
    ) -> Dict[str, float]:
        """
        计算 TF-IDF 相似度得分。

        Args:
            query: 查询文本
            documents: 文档列表 [(doc_id, text), ...]

        Returns:
            {doc_id: tfidf_score} 按得分降序排列
        """
        if not documents or not query:
            return {}

        query_tokens = cls._tokenize(query)
        if not query_tokens:
            return {}

        # 文档数量
        n_docs = len(documents)

        # 计算每个文档的 TF
        doc_tfs: Dict[str, Counter] = {}
        for doc_id, text in documents:
            doc_tfs[doc_id] = cls._compute_tf(cls._tokenize(text))

        # 计算 IDF: log(N / (1 + df))  df=包含该词的文档数
        doc_freq: Counter = Counter()
        for doc_id, tf in doc_tfs.items():
            for token in set(tf.keys()):
                doc_freq[token] += 1

        idf: Dict[str, float] = {}
        for token, df in doc_freq.items():
            idf[token] = math.log((n_docs + 1) / (1 + df)) + 1

        # 查询的 TF
        query_tf = cls._compute_tf(query_tokens)

        # 计算每个文档与查询的余弦相似度
        scores: Dict[str, float] = {}
        all_tokens = set(query_tf.keys())
        for doc_id, tf in doc_tfs.items():
            all_tokens.update(tf.keys())

        # 计算查询向量的模
        query_norm = math.sqrt(
            sum((query_tf.get(t, 0) * idf.get(t, 0)) ** 2 for t in query_tf)
        )
        if query_norm == 0:
            return {}

        # 计算每个文档与查询的余弦相似度
        for doc_id in doc_tfs:
            doc_tf = doc_tfs[doc_id]
            dot_product = 0.0
            for token in query_tf:
                if token in doc_tf:
                    dot_product += (query_tf[token] * idf.get(token, 0)) * \
                                   (doc_tf[token] * idf.get(token, 0))

            doc_norm = math.sqrt(
                sum((doc_tf.get(t, 0) * idf.get(t, 0)) ** 2 for t in doc_tf)
            ) if doc_tf else 0

            if doc_norm > 0:
                scores[doc_id] = dot_product / (query_norm * doc_norm)
            else:
                scores[doc_id] = 0.0

        return dict(sorted(scores.items(), key=lambda x: x[1], reverse=True))

    def search(
        self,
        query: str,
        session_id: str = "",
        category: str = "",
        limit: int = 10,
        mode: str = "hybrid",
    ) -> List[MemoryEntry]:
        """
        搜索记忆。

        支持三种搜索模式:
          - "keyword": 传统 LIKE 关键词匹配（快速）
          - "semantic": TF-IDF 语义搜索（理解语义相似性）
          - "hybrid": 混合搜索（默认）= 0.4 * keyword_score + 0.6 * tfidf_score

        Args:
            query: 搜索查询
            session_id: 会话 ID（空=所有会话）
            category: 记忆类别（空=所有类别）
            limit: 返回数量
            mode: 搜索模式 "keyword" | "semantic" | "hybrid"
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

        where = " AND ".join(conditions)

        if mode == "keyword":
            return self._search_keyword(conn, query, where, params, limit)
        elif mode == "semantic":
            return self._search_semantic(conn, query, where, params, limit)
        else:
            # 混合模式：取两种搜索结果的加权和
            keyword_results = self._search_keyword(conn, query, where, params, limit * 2)
            semantic_results = self._search_semantic(conn, query, where, params, limit * 2)

            # 合并评分
            combined: Dict[str, Tuple[MemoryEntry, float]] = {}
            for i, entry in enumerate(keyword_results):
                score = 1.0 - (i / max(len(keyword_results), 1))
                combined[entry.id] = (entry, score * 0.4)

            for i, entry in enumerate(semantic_results):
                score = 1.0 - (i / max(len(semantic_results), 1))
                if entry.id in combined:
                    combined[entry.id] = (entry, combined[entry.id][1] + score * 0.6)
                else:
                    combined[entry.id] = (entry, score * 0.6)

            # 按综合得分排序
            sorted_results = sorted(
                combined.values(),
                key=lambda x: x[1],
                reverse=True,
            )

            # 更新访问计数
            for entry, _ in sorted_results[:limit]:
                conn.execute(
                    "UPDATE memories SET access_count = access_count + 1 WHERE id = ?",
                    (entry.id,),
                )
            conn.commit()

            return [entry for entry, _ in sorted_results[:limit]]

    def _search_keyword(
        self,
        conn: sqlite3.Connection,
        query: str,
        where: str,
        params: list,
        limit: int,
    ) -> List[MemoryEntry]:
        """关键词 LIKE 搜索"""
        like_pattern = f"%{query}%"
        conditions = f"{where} AND (content LIKE ? OR summary LIKE ? OR key LIKE ?)"
        search_params = params + [like_pattern, like_pattern, like_pattern]

        sql = f"""
            SELECT * FROM memories WHERE {conditions}
            ORDER BY importance DESC, access_count DESC
            LIMIT ?
        """
        search_params.append(limit)
        rows = conn.execute(sql, search_params).fetchall()

        for row in rows:
            conn.execute(
                "UPDATE memories SET access_count = access_count + 1 WHERE id = ?",
                (row["id"],),
            )
        conn.commit()

        return [MemoryEntry.from_row(row) for row in rows]

    def _search_semantic(
        self,
        conn: sqlite3.Connection,
        query: str,
        where: str,
        params: list,
        limit: int,
    ) -> List[MemoryEntry]:
        """TF-IDF 语义搜索"""
        # 先取一批候选文档
        candidate_sql = f"SELECT * FROM memories WHERE {where} ORDER BY created_at DESC LIMIT 200"
        rows = conn.execute(candidate_sql, params).fetchall()

        if not rows:
            return []

        # 构建文档列表（content + summary + key 混合文本）
        documents = []
        row_map: Dict[str, sqlite3.Row] = {}
        for row in rows:
            doc_id = row["id"]
            text = f"{row['content']} {row['summary']} {row['key']}"
            documents.append((doc_id, text))
            row_map[doc_id] = row

        # 计算 TF-IDF 得分
        scores = self._compute_tfidf(query, documents)

        # 按得分排序，取 top N
        top_ids = list(scores.keys())[:limit]
        result = [MemoryEntry.from_row(row_map[doc_id]) for doc_id in top_ids if doc_id in row_map]

        # 更新访问计数
        for entry in result:
            conn.execute(
                "UPDATE memories SET access_count = access_count + 1 WHERE id = ?",
                (entry.id,),
            )
        conn.commit()

        return result

    def search_across_sessions(
        self,
        query: str,
        category: str = "",
        limit: int = 20,
        mode: str = "hybrid",
    ) -> List[MemoryEntry]:
        """跨会话搜索"""
        return self.search(query, session_id="", category=category, limit=limit, mode=mode)

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
