# Copyright (C) 2024 by Ivan.
# This file is part of Rocketgram, the modern Telegram bot framework.
# Rocketgram is released under the MIT License (see LICENSE).


import asyncio
import logging
import os
import signal
import sys
from argparse import ArgumentParser
from asyncio import StreamReader, StreamWriter, create_task, Task, Event
from contextlib import suppress
from typing import Tuple, Optional, Awaitable, Callable, List

from websockets import WebSocketCommonProtocol, ConnectionClosed
from websockets import client as ws

# Support for Python < 3.11
# Can cause unexpected behavior in some cases
# TBD: Remove this as well as asyncio.TimeoutError
if not hasattr(asyncio, "timeout"):
    class Timeout:
        def __init__(self, _):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass


    timeout = Timeout

    setattr(asyncio, "timeout", Timeout)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("whannel")
logger.setLevel(logging.DEBUG if sys.flags.debug else logging.INFO)

CONNECTION_TIMEOUT = 30
TOUCH_TIMEOUT = 5
READ_SIZE = 1024


class Base:
    def __init__(self):
        self._control: Optional[WebSocketCommonProtocol] = None

    @staticmethod
    async def _make_data_conn(url: str) -> WebSocketCommonProtocol:
        async with asyncio.timeout(CONNECTION_TIMEOUT):
            try:
                logger.debug("Trying to open data connection...")
                return await ws.connect(url)
            except Exception as e:
                logger.error(f"Failed to open data connection: {e}")
                await asyncio.sleep(1)

    @staticmethod
    def _make_rw(rd: StreamReader, wr: StreamWriter, ws: WebSocketCommonProtocol) -> Tuple[Task, Task]:
        async def reader():
            while not rd.at_eof():
                data = await rd.read(READ_SIZE)
                print("READ", data)
                await ws.send(data)

        async def writer():
            while not ws.closed:
                try:
                    data = await asyncio.wait_for(ws.recv(), timeout=TOUCH_TIMEOUT)
                    print("WRITE", data)
                    if not data:
                        raise TimeoutError
                    wr.write(data)
                except (asyncio.TimeoutError, TimeoutError):
                    await ws.send(b"")

        return asyncio.create_task(reader()), asyncio.create_task(writer())

    async def _make_control_conn(self, url: str) -> List[str]:
        """Opens control connection to the gate."""
        async with asyncio.timeout(CONNECTION_TIMEOUT):
            self._control = await ws.connect(url)
            msg = await self._control.recv()
            params = msg.split(" ")

            if params[0] != "OK":
                raise ConnectionError(f"Failed to connect to gate!")

        logger.info(f"Connected to gate!")

        return params

    async def accept_connection(self, host: str, port: int, url: str, gate_id: str, connection_id: str, connection_secret: str):
        client_writer: Optional[StreamWriter] = None
        reader: Optional[Task] = None
        writer: Optional[Task] = None
        connection = None

        try:
            connection_url = f"{url}connect/{gate_id}/acceptor/{connection_id}/{connection_secret}"

            connection = await self._make_data_conn(connection_url)
            logger.info(f"Established a new data connection {connection_id}!")

            logger.debug("Connecting to target... ")

            # client_reader, client_writer = await asyncio.open_connection(host, port)
            client_reader, client_writer = await asyncio.open_unix_connection(os.environ.get("SOCK_PATH", "./debug_pipe"))
            logger.info(f"Connected to target server {host}:{port}!")
            logger.info(f"Serving connection!")

            # Working with connection
            reader, writer = self._make_rw(client_reader, client_writer, connection)
            await asyncio.wait((reader, writer), return_when=asyncio.FIRST_COMPLETED)
            print("END!!!!")
        except (asyncio.TimeoutError, TimeoutError, ConnectionError, ConnectionClosed) as e:
            logger.error(f"Broken data connection: {e}")
            logger.exception(f"Broken data connection: {e}")
        except Exception as e:
            logger.error(f"Unexpected error in data connection: {e}")
        finally:
            if reader:
                reader.cancel()
                with suppress(BaseException):
                    await reader
            if writer:
                writer.cancel()
                with suppress(BaseException):
                    await writer
            if client_writer is not None:
                with suppress():
                    client_writer.close()
            if connection is not None:
                with suppress():
                    await connection.close()

            logger.info(f"Finished connection {connection_id}!")

    async def process_connection(self, stop_event: Event, rd: StreamReader, wr: StreamWriter, url: str, gate_id: str):
        connection: Optional[WebSocketCommonProtocol] = None
        reader: Optional[Task] = None
        writer: Optional[Task] = None
        connection_id = None

        try:
            await self._control.send("CONNECT")
            while True:
                connection_rep = await asyncio.wait_for(self._control.recv(), CONNECTION_TIMEOUT)
                if connection_rep != "TOUCH":
                    break
            print(connection_rep)
            rep, connection_id, secret = connection_rep.split(" ")

            if rep != "CONNECTION":
                raise ConnectionError(f"Wrong response: {connection_rep}")
        except (asyncio.TimeoutError, TimeoutError, ConnectionError, ConnectionClosed, ValueError) as e:
            print("SET STOP EVENT")
            stop_event.set()
            logger.error(f"Control connection is broken: {e}")
            return
        except Exception as e:
            print("SET STOP EVENT")
            stop_event.set()
            logger.error(f"Unexpected error in control connection: {type(e)}: {e}")
            return

        try:
            connection = await self._make_data_conn(f"{url}connect/{gate_id}/requestor/{connection_id}/{secret}")
            reader, writer = self._make_rw(rd, wr, connection)
            await asyncio.wait((reader, writer), return_when=asyncio.FIRST_COMPLETED)
        except (asyncio.TimeoutError, TimeoutError, ConnectionError, ConnectionClosed) as e:
            logger.error(f"Broken data connection: {e}")
        except Exception as e:
            logger.error(f"Unexpected error in data connection: {type(e)}: {e}")
        finally:
            if reader:
                reader.cancel()
                with suppress(BaseException):
                    await reader
            if writer:
                writer.cancel()
                with suppress(BaseException):
                    await writer
            with suppress():
                wr.close()
            if connection is not None:
                if connection:
                    with suppress():
                        await connection.close()

            if connection_id:
                logger.info(f"Finished connection {connection_id}!")
            else:
                logger.info(f"Finished unknown connection!")

    async def tcp_requestor(self, url: str, host: str, port: int, gate: str):
        gate_id = gate
        control_url = f"{url}associate/{gate_id}"

        logger.debug(f"Connecting to {control_url}...")

        stop_event = asyncio.Event()

        srv = None
        connections = []

        try:
            await self._make_control_conn(control_url)

            def _process(rd: StreamReader, wr: StreamWriter):
                connection = asyncio.create_task(self.process_connection(stop_event, rd, wr, url, gate_id))
                connections.append(connection)
                return connection

            # srv = await asyncio.start_server(_process, host=host, port=port)
            srv = await asyncio.start_unix_server(_process, path=os.environ.get("SOCK_PATH", "./debug_pipe"))

            while True:
                try:
                    await asyncio.wait_for(stop_event.wait(), TOUCH_TIMEOUT)
                    print("STOPPING EVENT!!!!")
                    return
                except (asyncio.TimeoutError, TimeoutError):
                    await self._control.send("TOUCH")
        except (asyncio.TimeoutError, TimeoutError, ConnectionError, ConnectionClosed) as e:
            logger.error(f"Broken connection: {e}")
        except Exception as e:
            logger.error(f"Unexpected error: {type(e)}: {e}")
        finally:
            if srv:
                with suppress():
                    srv.close()
            if self._control:
                with suppress():
                    await self._control.close()
            for conn in connections:
                conn.cancel()
                with suppress(BaseException):
                    await conn

            print("FINISHED!!!!")

    async def tcp_acceptor(self, url: str, host: str, port: int, secret: str):
        control_url = f"{url}create/{secret}"

        logger.debug(f"Connecting to {control_url}...")

        connections = []

        try:
            _, gate_id = await self._make_control_conn(control_url)

            logger.info(f"Created new gate {gate_id}!")

            while True:
                try:
                    connect_req = await asyncio.wait_for(self._control.recv(), TOUCH_TIMEOUT)

                    req, *params = connect_req.split(" ")

                    if req == "TOUCH":
                        raise TimeoutError

                    if req != "CONNECTION":
                        raise ConnectionError(f"Invalid request: {connect_req}")

                    connection_id, connection_secret = params

                    conn = create_task(self.accept_connection(host, port, url, gate_id, connection_id, connection_secret))
                    connections.append(conn)
                except (asyncio.TimeoutError, TimeoutError):
                    await self._control.send("TOUCH")

                    # Cleaning up broken connections
                    for conn in list(connections):
                        if conn.done():
                            connections.remove(conn)
                            with suppress():
                                await conn

        except (asyncio.TimeoutError, TimeoutError, ConnectionError, ConnectionClosed) as e:
            logger.error(f"Broken control connection: {e}")
        except Exception as e:
            logger.error(f"Unexpected error in control connection: {type(e)}: {e}")
        finally:
            with suppress():
                await self._control.close()
            for conn in connections:
                conn.cancel()
                with suppress(BaseException):
                    await conn


async def runner(worker: Callable[..., Awaitable], *args, **kwargs):
    stop = asyncio.Event()
    stop.clear()

    async def run():
        while True:
            try:
                await worker(*args, **kwargs)
            except asyncio.CancelledError:
                return
            except BaseException:
                pass

            logger.warning("Restarting...")
            await asyncio.sleep(1)

    loop = asyncio.get_event_loop()
    loop.add_signal_handler(signal.SIGINT, lambda: stop.set())

    task = asyncio.create_task(run())
    wait = asyncio.create_task(stop.wait())

    await asyncio.wait((task, wait), return_when=asyncio.FIRST_COMPLETED)

    wait.cancel()
    task.cancel()

    try:
        await task
    except BaseException:
        pass

    logger.info("Bye!")


def main():
    parser = ArgumentParser(prog="whannel", description="The websocket connections multiplexor gateway client.")
    parser.add_argument("url", help="Gateway connection URL.")

    suppress = parser.add_subparsers(title="Working mode", metavar="mode", required=True, dest="mode")

    requestor = suppress.add_parser("requestor", help="The requestor mode.")
    requestor.add_argument("gate", help="Gateway id.")
    requestor.add_argument("port", help="Port to listen.")
    requestor.add_argument("-n", "--host", default="localhost", help="Hostname/ip to listen.")

    acceptor = suppress.add_parser("acceptor", help="The acceptor mode.")
    acceptor.add_argument("host", help="Hostname/ip to connect to.")
    acceptor.add_argument("port", help="Port to connect to.")
    acceptor.add_argument("-s", "--secret", default="gate", help="The secret to use for authentication.")

    # sock_requestor = suppress.add_parser("sock-requestor", help="The sock requestor mode.")
    # sock_requestor.add_argument("gate", help="Gateway id.")
    # sock_requestor.add_argument("path", default="./connect.sock", help="Path to listen socket file.")
    #
    # sock_acceptor = suppress.add_parser("sock-acceptor", help="The sock acceptor mode.")
    # sock_acceptor.add_argument("path", help="Path to socket file to connect to.")

    args = vars(parser.parse_args())

    workers = {
        "requestor": Base().tcp_requestor,
        "acceptor": Base().tcp_acceptor,
        # "sock-requestor": sock_requestor,
        # "sock-acceptor": sock_acceptor,
    }

    if not (worker := workers.get(args.pop("mode"))):
        raise NotImplementedError(f"Mode is not implemented!")

    asyncio.run(runner(worker, **args))


if __name__ == "__main__":
    main()
