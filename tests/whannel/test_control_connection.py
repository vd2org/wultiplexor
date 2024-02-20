import asyncio
from asyncio import CancelledError
from contextlib import suppress
from unittest import mock

import pytest

from wultiplexor.whannel import ControlConnection


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("recv_vals", "expected_recv_count", "expected_queue_size", "expected_close_count"),
    [
        [[CancelledError], 1, 0, 0],
        [[Exception], 1, 0, 1],

        [[ControlConnection.TOUCH, CancelledError], 2, 0, 0],
        [[ControlConnection.TOUCH, Exception], 2, 0, 1],
        [["SOME_DATA", CancelledError], 2, 1, 0],
        [["SOME_DATA", Exception], 2, 1, 1],

        [[ControlConnection.TOUCH, "SOME_DATA", CancelledError], 3, 1, 0],
        [[ControlConnection.TOUCH, "SOME_DATA", Exception], 3, 1, 1],
        [["SOME_DATA", ControlConnection.TOUCH, CancelledError], 3, 1, 0],
        [["SOME_DATA", ControlConnection.TOUCH, Exception], 3, 1, 1],

        [[ControlConnection.TOUCH, "SOME_DATA", "SOME_DATA", CancelledError], 4, 2, 0],
        [[ControlConnection.TOUCH, "SOME_DATA", "SOME_DATA", Exception], 4, 2, 1],
        [["SOME_DATA", "SOME_DATA", ControlConnection.TOUCH, CancelledError], 4, 2, 0],
        [["SOME_DATA", "SOME_DATA", ControlConnection.TOUCH, Exception], 4, 2, 1],
        [["SOME_DATA", ControlConnection.TOUCH, "SOME_DATA", CancelledError], 4, 2, 0],
        [["SOME_DATA", ControlConnection.TOUCH, "SOME_DATA", Exception], 4, 2, 1],

        [["SOME_DATA", ControlConnection.TOUCH] * 1024 + [CancelledError], 2049, 1024, 0],
        [["SOME_DATA", ControlConnection.TOUCH] * 1024 + [Exception], 2049, 1024, 1],

        [[ControlConnection.TOUCH, CancelledError, ControlConnection.TOUCH], 2, 0, 0],
        [[ControlConnection.TOUCH, Exception, ControlConnection.TOUCH], 2, 0, 1],

        [["SOME_DATA", CancelledError, "SOME_DATA"], 2, 1, 0],
        [["SOME_DATA", Exception, "SOME_DATA"], 2, 1, 1],
    ]
)
async def test_work(recv_vals, expected_recv_count, expected_queue_size, expected_close_count):
    """Test the desired behavior of ControlConnection._work routine."""

    cc = ControlConnection("ws://localhost", "connect")

    control = mock.AsyncMock()
    mock.patch.object(control, "recv", side_effect=recv_vals).start()
    mock.patch.object(cc, "_control", control).start()

    close = mock.patch.object(cc, "close", return_value=None).start()
    mock.patch.object(cc, "send", return_value=None).start()

    queue = asyncio.Queue()
    mock.patch.object(cc, "_queue", queue).start()

    task = asyncio.create_task(cc._work())

    while not task.done():
        await asyncio.sleep(0)

    with suppress(asyncio.CancelledError):
        await task

    assert control.recv.await_count == expected_recv_count
    assert queue.qsize() == expected_queue_size
    assert close.await_count == expected_close_count
