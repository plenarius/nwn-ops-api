import asyncio
import logging

logger = logging.getLogger(__name__)


class TaskRegistry:
    def __init__(self):
        self._tasks: dict[str, asyncio.Task] = {}

    def is_running(self, name: str) -> bool:
        task = self._tasks.get(name)
        return task is not None and not task.done()

    def start(self, name: str, coro) -> asyncio.Task:
        if self.is_running(name):
            coro.close()
            raise RuntimeError(f"Operation '{name}' is already in progress")
        task = asyncio.create_task(coro, name=name)
        self._tasks[name] = task
        task.add_done_callback(lambda t: self._on_done(name, t))
        return task

    def _on_done(self, name: str, task: asyncio.Task):
        if not task.cancelled() and task.exception():
            logger.error("Background task '%s' failed: %s", name, task.exception())
        self._tasks.pop(name, None)

    def running_tasks(self) -> list[str]:
        return [name for name, task in self._tasks.items() if not task.done()]

    def cancel(self, name: str) -> bool:
        """Cancel a running operation (a restart waiting out its countdown). True when there was one."""
        task = self._tasks.get(name)
        if task is None or task.done():
            return False
        task.cancel()
        return True
