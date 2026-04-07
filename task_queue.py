"""
任务队列模块 - 异步任务调度
============================
支持任务优先级、超时控制、并发限制
"""
import queue
import time
import uuid
import logging
import threading
from typing import Any, Callable, Dict, Optional
from dataclasses import dataclass, field
from enum import IntEnum

logger = logging.getLogger("myagent.queue")


class Priority(IntEnum):
    LOW = 0
    NORMAL = 5
    HIGH = 10
    URGENT = 15


@dataclass(order=True)
class TaskItem:
    """任务项"""
    priority: int
    task_id: str = field(compare=False)
    func: Callable = field(compare=False)
    args: tuple = field(default_factory=tuple, compare=False)
    kwargs: Dict = field(default_factory=dict, compare=False)
    callback: Optional[Callable] = field(default=None, compare=False)
    timeout: float = field(default=300, compare=False)
    created_at: float = field(default_factory=time.time, compare=False)
    future: Any = field(default=None, compare=False)


class TaskQueue:
    """任务队列"""

    def __init__(self, max_workers: int = 3, max_queue_size: int = 100):
        self._queue: queue.PriorityQueue = queue.PriorityQueue(maxsize=max_queue_size)
        self._max_workers = max_workers
        self._workers: List[threading.Thread] = []
        self._stop_event = threading.Event()
        self._stats = {
            "enqueued": 0,
            "completed": 0,
            "failed": 0,
            "timeout": 0,
            "rejected": 0,
        }
        self._lock = threading.Lock()

    def start(self) -> None:
        """启动工作线程"""
        for i in range(self._max_workers):
            t = threading.Thread(
                target=self._worker_loop,
                daemon=True,
                name=f"task-worker-{i}"
            )
            t.start()
            self._workers.append(t)
        logger.info(f"TaskQueue: 启动 {self._max_workers} 个工作线程")

    def stop(self) -> None:
        """停止所有工作线程"""
        self._stop_event.set()
        for t in self._workers:
            t.join(timeout=5)
        logger.info("TaskQueue: 已停止")

    def enqueue(
        self,
        func: Callable,
        args: tuple = (),
        kwargs: Optional[Dict] = None,
        priority: Priority = Priority.NORMAL,
        timeout: float = 300,
        callback: Optional[Callable] = None,
    ) -> Optional[str]:
        """入队一个任务"""
        kwargs = kwargs or {}
        task_id = str(uuid.uuid4())

        task = TaskItem(
            priority=int(priority),
            task_id=task_id,
            func=func,
            args=args,
            kwargs=kwargs,
            callback=callback,
            timeout=timeout,
        )

        try:
            self._queue.put(task, block=False)
            with self._lock:
                self._stats["enqueued"] += 1
            return task_id
        except queue.Full:
            logger.warning(f"TaskQueue: 队列已满，任务被拒绝")
            with self._lock:
                self._stats["rejected"] += 1
            return None

    def _worker_loop(self) -> None:
        """工作线程循环"""
        while not self._stop_event.is_set():
            try:
                task = self._queue.get(timeout=1)
                self._execute_task(task)
                self._queue.task_done()
            except queue.Empty:
                continue
            except Exception as e:
                logger.error(f"TaskQueue 工作线程异常: {e}")

    def _execute_task(self, task: TaskItem) -> None:
        """执行任务"""
        start = time.time()
        try:
            result = task.func(*task.args, **task.kwargs)

            elapsed = time.time() - start
            with self._lock:
                self._stats["completed"] += 1

            if task.callback:
                try:
                    task.callback(result)
                except Exception as e:
                    logger.error(f"TaskQueue 回调异常: {e}")

        except Exception as e:
            with self._lock:
                self._stats["failed"] += 1
            logger.error(f"TaskQueue 任务执行失败 [{task.task_id}]: {e}")

    def size(self) -> int:
        return self._queue.qsize()

    def get_stats(self) -> Dict:
        with self._lock:
            return dict(self._stats)
