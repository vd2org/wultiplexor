# Copyright (C) 2024 by Vd.
# This file is part of Wultiplexor project.
# Wultiplexor is released under the MIT License (see LICENSE).


import asyncio
import dataclasses
import logging
import secrets
import signal
import string
import sys
import time
from argparse import ArgumentParser
from contextlib import suppress
from typing import Dict, Optional

from websockets import serve, WebSocketServerProtocol, ConnectionClosed
from websockets.frames import CloseCode

from .version import VERSION, PROTOCOL_VERSION

NAME = "wultiplexor"

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(NAME)
logger.setLevel(logging.DEBUG if sys.flags.debug else logging.INFO)
logging.getLogger("websockets").setLevel(logging.INFO if sys.flags.debug else logging.ERROR)

CONNECTION_TIMEOUT = 30
TOUCH_TIMEOUT = 5
TOKEN_LENGTH = getattr(secrets, "DEFAULT_ENTROPY", 32)


class AuthenticationError(Exception):
    """Raised when the client fails to authenticate."""


class AlreadyEstablishedConnectionError(Exception):
    """Raised when the client fails to authenticate."""


@dataclasses.dataclass
class Connection:
    id: str
    created_at: int
    associated_id: str
    requestor_secret: str
    acceptor_secret: str
    requestor: Optional[WebSocketServerProtocol]
    acceptor: Optional[WebSocketServerProtocol]


@dataclasses.dataclass
class Gate:
    id: str
    control: WebSocketServerProtocol
    associated: Dict[str, WebSocketServerProtocol]
    connections: Dict[str, Connection]


class GatesProvider:
    def __init__(self, secret: str, remote_addr_header: Optional[str], path_prefix: str):
        self._gates: Dict[str, Gate] = {}
        self._secret: str = secret
        self._remote_addr_header: Optional[str] = remote_addr_header
        self._prefix: Optional[str] = path_prefix

    async def auth(self, ws: WebSocketServerProtocol, secret: str):
        """Authenticates the client."""
        if not secrets.compare_digest(secret, self._secret):
            raise AuthenticationError

    @staticmethod
    def generate_gate_id(groups: int = 4, group_size: int = 4, alphabet: str = string.ascii_lowercase, separator: str = "_") -> str:
        """Generates a new human-readable gate id."""
        return separator.join(str().join(secrets.choice(alphabet) for _ in range(group_size)) for _ in range(groups))

    def remote_addr(self, ws: WebSocketServerProtocol) -> str:
        """Formats the remote address of the client."""
        client = None
        if self._remote_addr_header:
            client = ws.request_headers.get(self._remote_addr_header, None)
        return client or ":".join(map(str, ws.remote_address))

    async def _close(self, gate: Gate):
        with suppress():
            await gate.control.close()

        for associated in gate.associated.values():
            with suppress():
                await associated.close()

        for connection in gate.connections.values():
            with suppress():
                await connection.requestor.close()
            with suppress():
                await connection.acceptor.close()

    async def _create(self, ws: WebSocketServerProtocol, gate_id: str):
        """Creates a new gateway connection."""

        if existing := self._gates.pop(gate_id, None):
            logger.info(f"[{self.remote_addr(ws)}] Closing the existing gate...")
            await self._close(existing)

        self._gates[gate_id] = gate = Gate(id=gate_id, control=ws, associated={}, connections={})

        logger.info(f"[{self.remote_addr(ws)}] Creating a new gate {gate_id}...")

        try:
            await ws.send("OK")

            # Holding the connection: receiving data, ignoring it and sending a touch
            while True:
                try:
                    await asyncio.wait_for(ws.recv(), timeout=TOUCH_TIMEOUT)
                except TimeoutError:
                    logger.debug(f"[{self.remote_addr(ws)}] Idle gateway connection {gate_id}...")
                    await ws.send("TOUCH")
                    continue

                # TODO: TBDL: Cleanup idle connections for the gateway!

        except ConnectionClosed:
            logger.info(f"[{self.remote_addr(ws)}] Gateway {gate_id} connection is closed, cleaning up...")
        except Exception as e:
            logger.debug(f"[{self.remote_addr(ws)}] Error: {type(e)}: {e}")
            logger.info(f"[{self.remote_addr(ws)}] Gateway {gate_id} connection is broken, closing...")
        finally:
            await self._close(gate)
            with suppress():
                await ws.close()

    async def _associate(self, ws, gate_id):
        """Associates a new control connection with an existing gate."""
        association_id = None
        gate = None
        try:
            if not (gate := self._gates.get(gate_id)):
                raise ConnectionError("Invalid gate_id")

            while (association_id := secrets.token_urlsafe(TOKEN_LENGTH)) in gate.associated:
                pass

            gate.associated[association_id] = ws

            logger.info(f"[{self.remote_addr(ws)}] Creating a new associated connection {association_id} to gate {gate_id}...")

            await ws.send("OK")

            # Awaiting incoming connection requests
            while True:
                try:
                    command = await asyncio.wait_for(ws.recv(), timeout=TOUCH_TIMEOUT)
                except TimeoutError:
                    logger.debug(f"[{self.remote_addr(ws)}] Idle associated connection {association_id} for {gate_id}...")
                    await ws.send("TOUCH")
                    continue

                if command == "TOUCH":
                    continue

                if command != "CONNECT":
                    raise ConnectionError("Invalid request")

                while (connection_id := secrets.token_urlsafe(TOKEN_LENGTH)) in gate.connections:
                    pass

                connection = Connection(
                    id=connection_id,
                    created_at=int(time.time()),
                    associated_id=association_id,
                    requestor_secret=secrets.token_urlsafe(TOKEN_LENGTH),
                    acceptor_secret=secrets.token_urlsafe(TOKEN_LENGTH),
                    requestor=None,
                    acceptor=None
                )

                gate.connections[connection_id] = connection

                logger.info(f"[{self.remote_addr(ws)}] Created a new connection {connection_id} "
                            f"for gate {gate_id} associated with {association_id}")

                await ws.send(f"CONNECTION {connection_id} {connection.requestor_secret}")

                # We don't want to deal with gate connection errors here
                with suppress():
                    await gate.control.send(f"CONNECTION {connection_id} {connection.acceptor_secret}")
        except ConnectionClosed:
            logger.info(
                f"[{self.remote_addr(ws)}] Associated connection {association_id} for {gate_id} connection is closed, cleaning up...")
        except Exception as e:
            logger.debug(f"[{self.remote_addr(ws)}] Error: {type(e)}: {e}")
            logger.info(f"[{self.remote_addr(ws)}] Gateway {gate_id} connection is broken, closing...")
        finally:
            with suppress():
                await ws.close()
            if gate:
                to_clean = [gate.connections.pop(c.id) for c in list(gate.connections.values()) if c.associated_id == association_id]
                for connection in to_clean:
                    with suppress():
                        await connection.requestor.close()
                    with suppress():
                        await connection.acceptor.close()

    async def _connect(self, ws: WebSocketServerProtocol, gate_id: str, side: str, connection_id: str, secret: str):
        """Creates a new data connection."""
        connection = None
        gate = None
        try:
            if not (gate := self._gates.get(gate_id)):
                raise ConnectionError("Invalid gate")

            if not (connection := gate.connections.get(connection_id)):
                raise ConnectionError("Invalid gate")

            if all((connection.requestor, connection.acceptor)):
                raise AlreadyEstablishedConnectionError

            logger.info(f"[{self.remote_addr(ws)}] Creating a new data connection {connection_id} for "
                        f"gate {gate_id} associated with {connection.associated_id}...")

            required_secret = connection.requestor_secret if side == "requestor" else connection.acceptor_secret

            if not secrets.compare_digest(required_secret, secret):
                raise AuthenticationError

            # Assign the connection to the side
            setattr(connection, side, ws)

            async with asyncio.timeout(CONNECTION_TIMEOUT):
                while not all((connection.requestor, connection.acceptor)):
                    logger.debug(f"[{self.remote_addr(ws)}] Waiting for the data connection {connection_id} to be established...")
                    await asyncio.sleep(1)

            logger.info(f"Established data connection {connection_id} for gate_id: {gate_id} associated with {connection.associated_id}")

            opposite = next(iter(c for c in (connection.requestor, connection.acceptor) if c != ws))

            while True:
                await opposite.send(await ws.recv())

                try:
                    data = await asyncio.wait_for(ws.recv(), timeout=TOUCH_TIMEOUT)
                    if not data:
                        continue
                    await opposite.send(data)
                except TimeoutError:
                    logger.debug(f"[{self.remote_addr(ws)}] Idle data connection {connection_id}...")
                    await ws.send(b"")
                    continue
        except ConnectionClosed:
            logger.info(f"[{self.remote_addr(ws)}] Data connection {connection_id} for {gate_id} connection is closed, cleaning up...")
        except AlreadyEstablishedConnectionError:
            logger.error(f"[{self.remote_addr(ws)}] Connection {connection_id} already established!")
            gate = connection = None
            with suppress():
                await ws.close(code=CloseCode.POLICY_VIOLATION)
        except AuthenticationError:
            logger.error(f"[{self.remote_addr(ws)}] Authentication failed")
            gate = connection = None
            with suppress():
                await ws.close(code=CloseCode.POLICY_VIOLATION)
        except Exception as e:
            logger.debug(f"[{self.remote_addr(ws)}] Error: {type(e)}: {e}")
            logger.info(f"[{self.remote_addr(ws)}] Gateway {gate_id} connection is broken, closing...")
        finally:
            if gate:
                with suppress():
                    gate.connections.pop(connection.id, None)
            if connection:
                with suppress():
                    await connection.requestor.close()
                with suppress():
                    await connection.acceptor.close()

    async def handler(self, ws: WebSocketServerProtocol, path: str):
        """Parse the path and dispatch the request."""

        logger.debug(f"New connection from: {self.remote_addr(ws)} with path {path}")

        try:
            action, *splitted = path.removeprefix(self._prefix).removeprefix("/").split("/")

            if action == "connect":
                gate_id, side, connection_id, secret = splitted
                if side not in ("requestor", "acceptor"):
                    raise ValueError
                await self._connect(ws, gate_id, side, connection_id, secret)
            elif action == "associate":
                gate_id, = splitted
                await self._associate(ws, gate_id)
            elif action == "create":
                gate_id, secret = splitted
                await self.auth(ws, secret)
                await self._create(ws, gate_id)
            else:
                raise ValueError
        except AuthenticationError:
            logger.error(f"[{self.remote_addr(ws)}] Authentication failed")
            await ws.close(code=CloseCode.POLICY_VIOLATION)
        except ValueError:
            logger.error(f"[{self.remote_addr(ws)}] Malformed path: {path}")
            await ws.close(code=CloseCode.INVALID_DATA)
        except Exception as e:
            logger.error(f"[{self.remote_addr(ws)}] Unexpected error: {type(e)}: {e}")
            await ws.close(code=CloseCode.INVALID_DATA)


async def server(host: str, port: int, secret: str, prefix: str, header: Optional[str]):
    logger.info(f"Starting {NAME} version {VERSION}, protocol {PROTOCOL_VERSION}...")

    stop = asyncio.Event()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: stop.set())

    gp = GatesProvider(secret, header, prefix)
    try:
        async with serve(gp.handler, host=host, port=port):
            logger.info(f"Serving on ws://{host}:{port}/...")
            await stop.wait()
    except OSError as e:
        logger.error(f"{e}")

    logger.info("Exiting...")


def main():
    parser = ArgumentParser(
        prog=NAME,
        usage=f"%(prog)s -b 0.0.0.0 -p 8000 -s {secrets.token_urlsafe(32)}",
        description="The websocket connections multiplexor gateway server."
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}, protocol {PROTOCOL_VERSION}")

    parser.add_argument("-b", "--host", default="127.0.0.1", help="The host to bind to.")
    parser.add_argument("-p", "--port", default=8000, type=int, help="The port to bind to.")
    parser.add_argument("-s", "--secret", required=True, default=None, help="The secret to use for authentication.")
    parser.add_argument("-H", "--header", default=None, help="Remote address header to use for logging.")
    parser.add_argument("-P", "--prefix", default="", help="Prefix for the websocket path.")

    args = parser.parse_args()

    asyncio.run(server(**vars(args)))


if __name__ == "__main__":
    main()
