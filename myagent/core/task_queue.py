"""
core/task_queue.py - 任务队列管理
===================================
提供线程安全的异步任务队列，支持优先级、超时、取消操作。
"""
from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Coroutine, Optional, Dict, List
from datetime import datetime

from core.logger import get_logger
from core.utils import generate_id, timestamp

logger = get_logger("myagent.task_queue")


class TaskStatus(str, Enum):
    """任务状态"""
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


@dataclass
class TaskItem:
    """任务项"""
    id: str = field(default_factory=lambda: generate_id("task"))
    name: str = ""
    description: str = ""
    func: Optional[Callable] = None
    args: tuple = ()
    kwargs: dict = field(default_factory=dict)
    priority: int = 5                    # 1(最高) ~ 10(最低)
    status: str = TaskStatus.PENDING
    result: Any = None
    error: str = ""
    created_at: str = field(default_factory=timestamp)
    started_at: str = ""
    finished_at: str = ""
    timeout: int = 300                   # 超时秒数
    retry_count: int = 0
    max_retries: int = 2
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "status": self.status,
            "priority": self.priority,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "result": self.result,
            "error": self.error,
            "retry_count": self.retry_count,
        }


class TaskQueue:
    """
    异步任务队列。

    特性:
      - 优先级调度
      - 超时控制
      - 自动重试
      - 任务取消
      - 回调通知
      - 并发限制

    使用示例:
        queue = TaskQueue(max_workers=3)
        await queue.start()

        task = await queue.submit(
            name="测试任务",
            func=my_function,
            args=(arg1, arg2),
            priority=3,
        )

        result = await task.result
        print(f"状态: {task.status}, 结果: {task.result}")
    """

    def __init__(
        self,
        max_workers: int = 3,
        default_timeout: int = 300,
        default_retries: int = 2,
    ):
        self.max_workers = max_workers
        self.default_timeout = default_timeout
        self.default_retries = default_retries

        self._queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._tasks: Dict[str, TaskItem] = {}
        self._running_tasks: set = set()
        self._workers: List[asyncio.Task] = []
        self._lock = threading.Lock()
        self._running = False
        self._event_callbacks: List[Callable] = []

        # 统计
        self._total_submitted = 0
        self._total_completed = 0
        self._total_failed = 0

    async def start(self):
        """启动任务队列工作线程"""
        if self._running:
            return
        self._running = True
        for i in range(self.max_workers):
            worker = asyncio.create_task(self._worker(f"worker-{i}"))
            self._workers.append(worker)
        logger.info(f"任务队列已启动 (workers={self.max_workers})")

    async def stop(self, wait: bool = True):
        """停止任务队列"""
        self._running = False
        # 取消所有 pending 任务
        for task in self._tasks.values():
            if task.status == TaskStatus.PENDING:
                task.status = TaskStatus.CANCELLED

        if wait:
            for worker in self._workers:
                if not worker.done():
                    worker.cancel()
                    try:
                        await worker
                    except asyncio.CancelledError:
                        pass
        self._workers.clear()
        logger.info("任务队列已停止")

    async def submit(
        self,
        func: Callable,
        name: str = "",
        description: str = "",
        args: tuple = (),
        kwargs: Optional[dict] = None,
        priority: int = 5,
        timeout: Optional[int] = None,
        max_retries: Optional[int] = None,
        callback: Optional[Callable] = None,
        metadata: Optional[dict] = None,
    ) -> TaskItem:
        """
        提交任务到队列。

        Args:
            func: 要执行的异步/同步函数
            name: 任务名称
            description: 任务描述
            args: 位置参数
            kwargs: 关键字参数
            priority: 优先级 (1=最高, 10=最低)
            timeout: 超时秒数
            max_retries: 最大重试次数
            callback: 完成回调
            metadata: 附加元数据

        Returns:
            TaskItem 对象
        """
        task = TaskItem(
            name=name or func.__name__,
            description=description,
            func=func,
            args=args,
            kwargs=kwargs or {},
            priority=priority,
            timeout=timeout or self.default_timeout,
            max_retries=max_retries or self.default_retries,
            metadata=metadata or {},
        )
        self._tasks[task.id] = task
        self._total_submitted += 1
        await self._queue.put((task.priority, task.created_at, task.id))
        logger.debug(f"任务已提交: {task.name} (id={task.id}, priority={task.priority})")
        return task

    async def get_task(self, task_id: str) -> Optional[TaskItem]:
        """获取任务状态"""
        return self._tasks.get(task_id)

    async def cancel_task(self, task_id: str) -> bool:
        """取消任务"""
        task = self._tasks.get(task_id)
        if task and task.status in (TaskStatus.PENDING, TaskStatus.RUNNING):
            task.status = TaskStatus.CANCELLED
            logger.info(f"任务已取消: {task.name} (id={task_id})")
            return True
        return False

    async def wait_for_task(self, task_id: str, timeout: Optional[int] = None) -> Optional[TaskItem]:
        """等待任务完成"""
        start = time.time()
        while True:
            task = self._tasks.get(task_id)
            if task and task.status in (
                TaskStatus.SUCCESS, TaskStatus.FAILED,
                TaskStatus.TIMEOUT, TaskStatus.CANCELLED,
            ):
                return task
            if timeout and (time.time() - start) > timeout:
                return self._tasks.get(task_id)
            await asyncio.sleep(0.1)

    def get_all_tasks(self) -> List[Dict]:
        """获取所有任务状态"""
        return [t.to_dict() for t in self._tasks.values()]

    def get_stats(self) -> Dict[str, int]:
        """获取队列统计"""
        return {
            "total_submitted": self._total_submitted,
            "total_completed": self._total_completed,
            "total_failed": self._total_failed,
            "pending": sum(1 for t in self._tasks.values() if t.status == TaskStatus.PENDING),
            "running": sum(1 for t in self._tasks.values() if t.status == TaskStatus.RUNNING),
        }

    def on_event(self, callback: Callable):
        """注册事件回调"""
        self._event_callbacks.append(callback)

    def _notify(self, task: TaskItem, event: str):
        """通知事件回调"""
        for cb in self._event_callbacks:
            try:
                cb(task, event)
            except Exception as e:
                logger.error(f"事件回调执行失败: {e}")

    async def _worker(self, name: str):
        """工作线程主循环"""
        logger.debug(f"{name} 已启动")
        while self._running:
            try:
                # 从队列取任务(带超时，防止无法退出)
                try:
                    _, _, task_id = await asyncio.wait_for(
                        self._queue.get(), timeout=1.0
                    )
                except asyncio.TimeoutError:
                    continue

                task = self._tasks.get(task_id)
                if not task or task.status == TaskStatus.CANCELLED:
                    continue

                await self._execute_task(task)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"{name} 异常: {e}")

        logger.debug(f"{name} 已退出")

    async def _execute_task(self, task: TaskItem):
        """执行单个任务(含重试和超时)"""
        task.status = TaskStatus.RUNNING
        task.started_at = timestamp()
        self._notify(task, "start")
        logger.info(f"开始执行任务: {task.name} (id={task.id})")

        while task.retry_count <= task.max_retries:
            try:
                if asyncio.iscoroutinefunction(task.func):
                    result = await asyncio.wait_for(
                        task.func(*task.args, **task.kwargs),
                        timeout=task.timeout,
                    )
                else:
                    loop = asyncio.get_event_loop()
                    result = await asyncio.wait_for(
                        loop.run_in_executor(
                            None,
                            lambda: task.func(*task.args, **task.kwargs),
                        ),
                        timeout=task.timeout,
                    )

                task.result = result
                task.status = TaskStatus.SUCCESS
                self._total_completed += 1
                self._notify(task, "success")
                logger.info(f"任务完成: {task.name} (id={task.id})")
                return

            except asyncio.TimeoutError:
                task.retry_count += 1
                if task.retry_count > task.max_retries:
                    task.status = TaskStatus.TIMEOUT
                    task.error = f"执行超时 ({task.timeout}s)，已重试 {task.max_retries} 次"
                    self._total_failed += 1
                    self._notify(task, "timeout")
                    logger.error(f"任务超时: {task.name} (id={task.id})")
                    return
                logger.warning(f"任务超时，重试 {task.retry_count}/{task.max_retries}: {task.name}")

            except Exception as e:
                task.retry_count += 1
                task.error = str(e)
                if task.retry_count > task.max_retries:
                    task.status = TaskStatus.FAILED
                    task.error = f"{str(e)} (已重试 {task.max_retries} 次)"
                    self._total_failed += 1
                    self._notify(task, "failed")
                    logger.error(f"任务失败: {task.name} (id={task.id}) - {e}")
                    return
                logger.warning(f"任务异常，重试 {task.retry_count}/{task.max_retries}: {task.name} - {e}")

        task.finished_at = timestamp()
