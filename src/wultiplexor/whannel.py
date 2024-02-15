# Copyright (C) 2024 by Ivan.
# This file is part of Wultiplexor project.
# Wultiplexor is released under the MIT License (see LICENSE).


import asyncio
import logging
import signal
import sys
from argparse import ArgumentParser
from asyncio import StreamReader, StreamWriter, Task, CancelledError, QueueFull
from asyncio.base_events import Server
from contextlib import suppress
from typing import Optional, Awaitable, Callable, List, Tuple

from websockets import WebSocketCommonProtocol
from websockets import client as ws

from .version import VERSION, PROTOCOL_VERSION

NAME = "whannel"

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(NAME)
logger.setLevel(logging.DEBUG if sys.flags.debug else logging.INFO)
logging.getLogger("websockets").setLevel(logging.DEBUG if sys.flags.debug else logging.ERROR)

RESTART_TIMEOUT = 3
CONNECTION_TIMEOUT = 30
TOUCH_TIMEOUT = 5
READ_SIZE = 65535


class ControlConnectionError(ConnectionError):
    pass


class ControlConnection:
    TOUCH = "TOUCH"

    def __init__(self, base: str, *parts: str,
                 close_event: Optional[asyncio.Event] = None,
                 connection_timeout: int = CONNECTION_TIMEOUT,
                 touch_timeout: int = TOUCH_TIMEOUT,
                 queue_size: int = 100):
        self._url = base + "/".join(parts)
        self._connection_timeout = connection_timeout
        self._touch_timeout = touch_timeout
        self._control: Optional[WebSocketCommonProtocol] = None
        self._queue = asyncio.Queue(maxsize=queue_size)
        self._task: Optional[Task] = None
        self._wait_lock: asyncio.Lock = asyncio.Lock()
        self._was_opened: bool = False
        self._close_called: bool = False

        self._closed_event = close_event

    async def open(self):
        if self._was_opened:
            raise RuntimeError("Control connection is already opened!")
        self._was_opened = True

        async def _do():
            while True:
                try:
                    logger.info(f"Trying to open control connection to {self._url}...")
                    self._control = await ws.connect(self._url)
                    logger.info(f"Control connection established successfully!")
                    return
                except CancelledError:
                    raise
                except Exception as e:
                    logger.error(f"Failed to open control connection: {type(e)}: {e}, trying again...")
                    await asyncio.sleep(1)

        try:
            await asyncio.wait_for(_do(), self._connection_timeout)
            self._task = asyncio.create_task(self._work())
            return self
        except CancelledError:
            raise
        except (asyncio.TimeoutError, TimeoutError):
            await self.close()
            raise ControlConnectionError(f"Failed to connect to gate for {self._connection_timeout} seconds!") from None

    async def _work(self):
        while True:
            try:
                msg = await asyncio.wait_for(self._control.recv(), self._touch_timeout)
                if msg.startswith(self.TOUCH):
                    logger.debug("Received touch message from control connection!")
                    continue

                if self._queue.full():
                    logger.error("Control connection queue is full!")
                    raise ConnectionError

                await self._queue.put(msg)
            except CancelledError:
                raise
            except (asyncio.TimeoutError, TimeoutError):
                logger.debug("Sending touch message to control connection!")
                await self._control.send(self.TOUCH)
            except Exception as e:
                logger.error(f"Control connection is broken: {type(e)}: {e}")
                await self.close()
                return

    async def send(self, *msg_part: str):
        if not self._task or not self._control:
            await self.close()
            raise ControlConnectionError("Control connection is broken!")

        try:
            if not len(msg_part):
                return

            msg = " ".join(msg_part)
            logger.debug(f"Sending message {msg} to control connection!")
            await asyncio.wait_for(self._control.send(msg), self._connection_timeout)
            return self
        except CancelledError:
            raise
        except ControlConnectionError:
            await self.close()
            raise
        except (asyncio.TimeoutError, TimeoutError):
            logger.error("Control connection is broken: timeout to wait response!")
            await self.close()
            raise ControlConnectionError from None
        except Exception as e:
            logger.error(f"Control connection is broken: {type(e)}: {e}")
            await self.close()
            raise ControlConnectionError(f"Control connection is broken: {type(e)}: {e}") from None

    async def recv(self, expected_prefix: str, expected_parts: int = 0, forever: bool = False) -> Optional[List[str]]:
        if not self._task or not self._control:
            await self.close()
            raise ControlConnectionError("Control connection is broken!")

        if self._wait_lock.locked():
            raise RuntimeError("Another call waiting a response!")

        try:
            await self._wait_lock.acquire()
            msg = await self._queue.get() if forever else await asyncio.wait_for(self._queue.get(), self._connection_timeout)
            if msg in (..., None):
                raise ControlConnectionError
            parts = msg.split(" ")

            if not parts[0] == expected_prefix or len(parts) != expected_parts + 1:
                logger.error(f"Invalid response: {msg} while waiting for {expected_prefix}/{expected_parts}!")
                raise ControlConnectionError(f"Invalid response: {msg} while waiting for {expected_prefix}/{expected_parts}!")

            return parts[1:]
        except CancelledError:
            raise
        except ControlConnectionError:
            await self.close()
            raise
        except Exception as e:
            logger.error(f"Control connection is broken: {type(e)}: {e}")
            await self.close()
            raise ControlConnectionError from None
        finally:
            self._wait_lock.release()

    async def close(self):
        if not self._was_opened:
            raise RuntimeError("Control connection was not opened!")
        if self._close_called:
            return
        self._close_called = True

        if self._queue:
            with suppress(QueueFull):
                q = self._queue
                self._queue = None
                q.put_nowait(...)
        if self._task:
            t = self._task
            self._task = None
            t.cancel()
            with suppress(CancelledError, asyncio.TimeoutError, TimeoutError):
                await asyncio.wait_for(t, 1)
            self._task = None
        if self._control:
            c = self._control
            self._control = None
            with suppress():
                await asyncio.wait_for(c.close(), 1)

        if self._closed_event:
            self._closed_event.set()

        logger.info(f"Data connection closed!")


class DataConnectionError(ConnectionError):
    pass


class DataConnection:
    def __init__(self, base: str, *parts: str,
                 connection_timeout: int = CONNECTION_TIMEOUT,
                 touch_timeout: int = TOUCH_TIMEOUT):
        self._url = base + "/".join(parts)
        self._connection_timeout = connection_timeout
        self._touch_timeout = touch_timeout

        self._task: Optional[Task] = None
        self._data: Optional[WebSocketCommonProtocol] = None

        self._was_opened: bool = False
        self._was_served: bool = False
        self._close_called: bool = False

    async def open(self):
        if self._was_opened:
            raise RuntimeError("Data connection is already opened!")
        self._was_opened = True

        async def _do():
            while True:
                try:
                    logger.info(f"Trying to open data connection to {self._url}...")
                    self._data = await ws.connect(self._url)
                    logger.info(f"Data connection established successfully!")
                    return
                except CancelledError:
                    raise
                except Exception as e:
                    logger.error(f"Failed to open data connection: {type(e)}: {e}, trying again...")
                    await asyncio.sleep(1)

        try:
            await asyncio.wait_for(_do(), self._connection_timeout)
            return self
        except CancelledError:
            raise
        except (asyncio.TimeoutError, TimeoutError):
            await self.close()
            raise ControlConnectionError(f"Failed to open data connection for {self._connection_timeout} seconds!") from None

    def serve(self, rd: StreamReader, wr: StreamWriter):
        if not self._was_opened:
            raise RuntimeError("The data connection was never opened!")
        if self._close_called:
            raise RuntimeError("The data connection is closed!")
        if self._was_served:
            raise RuntimeError("The data connection already served!")
        self._was_served = True

        async def _serve():
            async def reader():
                while not rd.at_eof():
                    data = await rd.read(READ_SIZE)
                    if sys.flags.debug:
                        logger.debug(f"Send data: {data}")
                    await self._data.send(data)

                raise ConnectionError

            async def writer():
                while not self._data.closed:
                    try:
                        data = await asyncio.wait_for(self._data.recv(), timeout=TOUCH_TIMEOUT)
                        if sys.flags.debug:
                            logger.debug(f"Recv data: {data}")
                        if not data:
                            raise TimeoutError
                        wr.write(data)
                    except (asyncio.TimeoutError, TimeoutError):
                        logger.debug(f"Touching idle data connection.")
                        await self._data.send(b"")

                raise ConnectionError

            r = asyncio.create_task(reader())
            w = asyncio.create_task(writer())

            try:
                await asyncio.wait((r, w), return_when=asyncio.FIRST_EXCEPTION)
            finally:
                wr.close()
                r.cancel()
                w.cancel()

                await self.close()

        self._task = asyncio.create_task(_serve())
        return self

    @property
    def alive(self) -> bool:
        return self._was_opened and self._was_served and not self._close_called

    async def close(self):
        if not self._was_opened:
            raise RuntimeError("Control connection was not opened!")
        if self._close_called:
            return
        self._close_called = True

        if self._task:
            t = self._task
            self._task = None
            t.cancel()
            with suppress(CancelledError, asyncio.TimeoutError, TimeoutError):
                await asyncio.wait_for(t, 1)
            self._task = None

        if self._data:
            c = self._data
            self._data = None
            with suppress():
                await asyncio.wait_for(c.close(), 1)

        self._was_served = True
        logger.info(f"Data connection closed!")


class Base:
    def __init__(self, url: str):
        self._url: str = url
        self._control: Optional[ControlConnection] = None
        self._connections: List[DataConnection] = []
        self._cleanup_lock = asyncio.Lock()

    async def serve(self):
        raise NotImplementedError

    async def _cleanup_connections(self):
        for conn in self._connections.copy():
            if not conn.alive:
                logger.debug("Cleaning up one dead data connection.")
                with suppress(ValueError):
                    self._connections.remove(conn)
                await conn.close()

    async def _cleanup(self):
        if self._cleanup_lock.locked():
            return
        async with self._cleanup_lock:
            if self._control:
                c = self._control
                self._control = None
                await c.close()
            if self._connections:
                cc = self._connections
                self._connections = []
                for conn in cc:
                    await conn.close()


class BaseRequestor(Base):
    def __init__(self, url: str, gate: str):
        super().__init__(url)
        self._stop_event: Optional[asyncio.Event] = None
        self._gate_id: str = gate

    async def _listen(self, process: Callable[[StreamReader, StreamWriter], Awaitable]) -> Server:
        raise NotImplementedError

    async def _process(self, rd: StreamReader, wr: StreamWriter):
        connection: Optional[DataConnection] = None
        connection_id = "UNKNOWN"

        try:
            await self._control.send("CONNECT")
            connection_id, secret = await self._control.recv("CONNECTION", 2)

            connection = await DataConnection(self._url, "connect", self._gate_id, "requestor", connection_id, secret).open()
            logger.info(f"Established a new data connection {connection_id}!")
            connection.serve(rd, wr)
            await self._cleanup_connections()
            self._connections.append(connection)
        except ControlConnectionError:
            logger.debug(f"Control connection is dead!")
            if self._stop_event:
                self._stop_event.set()
            if connection:
                await connection.close()
        except DataConnectionError:
            logger.error(f"Broken data connection {connection_id}!")
            if connection:
                await connection.close()
        except Exception as e:
            logger.error(f"Unexpected error in data connection {connection_id}: {e}")

    async def serve(self):
        self._stop_event = asyncio.Event()
        srv = None

        try:
            self._control = await ControlConnection(self._url, "associate", self._gate_id, close_event=self._stop_event).open()
            await self._control.recv("OK")

            srv = await self._listen(self._process)

            await self._stop_event.wait()
        except ControlConnectionError:
            logger.debug(f"Control connection is dead!")
        except Exception as e:
            logger.error(f"Unexpected error: {type(e)}: {e}")
        finally:
            self._stop_event = None
            if srv:
                with suppress():
                    srv.close()
            await self._cleanup()


class BaseAcceptor(Base):
    def __init__(self, url: str, secret: str, ident: str, gate: str):
        super().__init__(url)
        self._secret: str = secret

        self._gate_id: Optional[str] = gate

        self._ident: Optional[str] = ident

    async def _connect(self) -> Tuple[StreamReader, StreamWriter]:
        raise NotImplementedError

    async def _accept(self, connection_id: str, connection_secret: str):
        connection: Optional[DataConnection] = None

        try:
            connection = await DataConnection(self._url, "connect", self._gate_id, "acceptor", connection_id, connection_secret).open()
            logger.info(f"Established a new data connection {connection_id}!")

            logger.debug("Connecting to target... ")
            rd, wr = await self._connect()
            logger.info(f"Connected to the target server {self._ident}. Serving connection!")
            self._connections.append(connection)
            connection.serve(rd, wr)
        except DataConnectionError:
            logger.error(f"Broken data connection {connection_id}!")
            if connection:
                await connection.close()
        except Exception as e:
            logger.error(f"Unexpected error in data connection {connection_id}: {e}")
            if connection:
                await connection.close()

    async def serve(self):
        try:
            self._control = await ControlConnection(self._url, "create", self._gate_id, self._secret).open()
            await self._control.recv("OK")

            while True:
                connection_id, secret = await self._control.recv("CONNECTION", 2, forever=True)
                await self._accept(connection_id, secret)

                await self._cleanup_connections()
        except ControlConnectionError:
            logger.debug(f"Control connection is dead!")
            return
        except Exception as e:
            logger.error(f"Unexpected error in control connection: {type(e)}: {e}")
            return
        finally:
            await self._cleanup()


class TcpRequestor(BaseRequestor):
    def __init__(self, url: str, gate: str, host: str, port: int):
        super().__init__(url, gate)
        self._host: str = host
        self._port: int = port

    async def _listen(self, process: Callable[[StreamReader, StreamWriter], Awaitable]) -> Server:
        return await asyncio.start_server(process, host=self._host, port=self._port)


class TcpAcceptor(BaseAcceptor):
    def __init__(self, url: str, secret: str, host: str, port: int, gate: str):
        super().__init__(url, secret, f"{host}:{port}", gate)
        self._host: str = host
        self._port: int = port

    async def _connect(self) -> Tuple[StreamReader, StreamWriter]:
        return await asyncio.open_connection(host=self._host, port=self._port)


class SockRequestor(BaseRequestor):
    def __init__(self, url: str, gate: str, path: str):
        super().__init__(url, gate)
        self._path: str = path

    async def _listen(self, process: Callable[[StreamReader, StreamWriter], Awaitable]) -> Server:
        return await asyncio.start_unix_server(process, path=self._path)


class SockAcceptor(BaseAcceptor):
    def __init__(self, url: str, secret: str, path: str, gate: str):
        super().__init__(url, secret, path, gate)
        self._path: str = path

    async def _connect(self) -> Tuple[StreamReader, StreamWriter]:
        # client_reader, client_writer = await asyncio.open_connection(host, port)
        return await asyncio.open_unix_connection(self._path)


async def runner(worker: Callable[..., Awaitable], *args, **kwargs):
    logger.info(f"Starting {NAME} version {VERSION}, protocol {PROTOCOL_VERSION}...")

    stop = asyncio.Event()

    async def run():
        while True:
            try:
                await worker(*args, **kwargs)
            except CancelledError:
                raise
            else:
                await asyncio.sleep(0)
                logger.warning("Restarting...")
                await asyncio.sleep(RESTART_TIMEOUT)

    loop = asyncio.get_event_loop()
    loop.add_signal_handler(signal.SIGINT, lambda: stop.set())

    task = asyncio.create_task(run())

    await stop.wait()

    logger.info("Exiting...")

    task.cancel()

    with suppress(CancelledError, asyncio.TimeoutError, TimeoutError):
        logger.debug("Waiting for worker to finish...")
        await asyncio.wait_for(task, 1)

    logger.info("Bye!")


def main():
    parser = ArgumentParser(prog=NAME, description="The websocket connections multiplexor gateway client.")

    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}, protocol {PROTOCOL_VERSION}")

    parser.add_argument("url", help="Gateway connection URL.")

    subs = parser.add_subparsers(title="Working mode", metavar="mode", required=True, dest="mode")

    requestor = subs.add_parser("requestor", help="The requestor mode.")
    requestor.add_argument("gate", help="Gateway id.")
    requestor.add_argument("port", type=int, help="Port to listen.")
    requestor.add_argument("-n", "--host", default="localhost", help="Hostname/ip to listen.")

    acceptor = subs.add_parser("acceptor", help="The acceptor mode.")
    acceptor.add_argument("host", help="Hostname/ip to connect to.")
    acceptor.add_argument("port", type=int, help="A port to connect to.")
    acceptor.add_argument("gate", type=str, help="A name of the gate.")
    acceptor.add_argument("-s", "--secret", default="gate", help="The secret to use for authentication.")

    sock_requestor = subs.add_parser("sock-requestor", help="The sock requestor mode.")
    sock_requestor.add_argument("gate", help="Gateway id.")
    sock_requestor.add_argument("path", default="./connect.sock", help="Path to listen socket file.")

    sock_acceptor = subs.add_parser("sock-acceptor", help="The sock acceptor mode.")
    sock_acceptor.add_argument("path", help="Path to a socket file to connect to.")
    sock_acceptor.add_argument("gate", type=str, help="A name of the gate.")
    sock_acceptor.add_argument("-s", "--secret", default="gate", help="The secret to use for authentication.")

    args = vars(parser.parse_args())

    workers = {
        "requestor": TcpRequestor,
        "acceptor": TcpAcceptor,
        "sock-requestor": SockRequestor,
        "sock-acceptor": SockAcceptor,
    }

    if not (worker := workers.get(args.pop("mode"))):
        raise NotImplementedError(f"Mode is not implemented!")

    asyncio.run(runner(worker(**args).serve))


if __name__ == "__main__":
    main()
