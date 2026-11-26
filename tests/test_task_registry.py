import asyncio

import pytest

from app.task_registry import TaskRegistry


@pytest.mark.asyncio
async def test_one_at_a_time_and_cancel():
    tasks = TaskRegistry()
    gate = asyncio.Event()
    tasks.start("restart", gate.wait())
    assert tasks.is_running("restart") and tasks.running_tasks() == ["restart"]
    with pytest.raises(RuntimeError):
        tasks.start("restart", gate.wait())
    assert tasks.cancel("restart") is True
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert not tasks.is_running("restart") and tasks.cancel("restart") is False
