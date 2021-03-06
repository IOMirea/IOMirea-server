"""
IOMirea-server - A server for IOMirea messenger
Copyright (C) 2019  Eugene Ershov

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU Affero General Public License as
published by the Free Software Foundation, either version 3 of the
License, or (at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU Affero General Public License for more details.

You should have received a copy of the GNU Affero General Public License
along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""

from __future__ import annotations

import json
import enum
import time
import asyncio

from typing import Any, Dict, Optional, Set

from aiohttp import web

from log import server_log
from models.access_token import Token
from models.events import Event, LocalEvent, OuterEvent, GlobalEvent


HEARTBEAT_INTERVAL = 30000


class Opcode(enum.Enum):
    DISPATCH = 0
    HEARTBEAT = 1
    IDENTIFY = 2
    PRESENCE = 3
    RESUME = 4
    RECONNECT = 5
    REQUEST_USERS = 6
    INVALIDATE_SESSION = 7
    HELLO = 8
    HEARTBEAT_ACK = 9


class CloseCode(enum.Enum):
    NORMAL = 1000
    UNKNOWN_OPCODE = 4001
    BAD_PAYLOAD = 4002
    NOT_IDENTIFIED = 4003
    BAD_TOKEN = 4004


class Listener:
    """Manages a single websocket."""

    __slots__ = ("ws", "user_id", "_emitter", "_last_hb")

    def __init__(self, ws: web.WebSocketResponse, emitter: EventEmitter):
        self.ws = ws
        self.user_id: Optional[int] = None

        self._emitter = emitter

        self._last_hb = time.time()

    async def notify(
        self, *, opcode: Opcode, data: Optional[Any] = None
    ) -> bool:
        """Sends data to websocket."""

        try:
            if data is None:
                await self.ws.send_json({"op": opcode.value})
            else:
                await self.ws.send_json({"op": opcode.value, "d": data})
        except RuntimeError:  # ws closed (dirty)
            server_log.debug("WS closed by user (dirty)")

            return False

        return True

    async def event_notify(self, event: Event) -> bool:
        """Sends dispatch message to websocket with event payload."""

        try:
            await self.ws.send_json(
                {
                    "op": Opcode.DISPATCH.value,
                    "d": event.payload,
                    "t": event.name,
                }
            )
        except RuntimeError:  # ws closed (dirty)
            server_log.debug("WS closed by user (dirty)")

            return False

        return True

    async def listen(self) -> None:
        """Starts handling websocket messages and launches heartbeat."""

        await self.notify(
            opcode=Opcode.HELLO,
            data={"heartbeat_interval": HEARTBEAT_INTERVAL},
        )

        asyncio.create_task(self._check_hb())

        async for msg in self.ws:
            # TODO: check message type
            try:
                await self._handle(json.loads(msg.data))
            except KeyError:
                await self.close(code=CloseCode.BAD_PAYLOAD)

    async def _handle(self, data: Dict[str, Any]) -> None:
        """Handles message from websocket."""

        server_log.debug(f"Received ws: {data}")

        op = data["op"]
        if op == Opcode.HEARTBEAT.value:
            self._last_hb = time.time()
            await self.notify(opcode=Opcode.HEARTBEAT_ACK)

        elif op == Opcode.IDENTIFY.value:
            try:
                token = Token.from_string(
                    data["d"]["token"], self._emitter._app["pg_conn"]
                )
                if not await token.verify():  # ValueError possible
                    raise ValueError
            except (ValueError, RuntimeError):
                await self.notify(opcode=Opcode.INVALIDATE_SESSION)
                await self.close(code=CloseCode.BAD_TOKEN)

                return

            self.user_id = token.user_id

            await self._emitter.add_listener(self)

    async def _check_hb(self) -> None:
        """
        A task that checks for user heartbeat responses every
        HEARTBEAT_INTERVAL milliseconds with one tenth precision.
        """

        interval = HEARTBEAT_INTERVAL / 1000
        response_error_treshold = interval / 10

        while not self.ws.closed:
            await asyncio.sleep(interval)

            response_error = (time.time() - self._last_hb) - interval

            if response_error > response_error_treshold:
                server_log.debug(f"Heartbeat: expired for user {self.user_id}")

                break

        server_log.debug(f"Heartbeat: closing listener {self}")

        await self.close()

    async def close(
        self,
        *,
        code: CloseCode = CloseCode.NORMAL,
        message: bytes = b"",
        cleanup: bool = True,
    ) -> None:
        """Closes connection with websocket."""

        if cleanup:
            await self._emitter.remove_listener(self)

        await self.ws.close(code=code.value, message=message)

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} user_id={self.user_id}>"


class EventEmitter:
    """Websocket manager responsible for event delivery."""

    def __init__(self, app: web.Application):
        self._app = app

        # maps users to all their channels
        self._channels: Dict[int, Set[int]] = {}

        # maps channels to all their users
        self._users: Dict[int, Set[int]] = {}

        # maps users to all their listeners
        self._listeners: Dict[int, Set[Listener]] = {}

        # set to True when closing to prevent new connections
        self._closing = False

        self._lock = asyncio.Lock()

    @staticmethod
    async def setup_emitter(app: web.Application) -> None:
        """Creates emitter property in application."""

        emitter = EventEmitter(app)

        app["emitter"] = emitter
        app.on_cleanup.append(emitter.close)

    def emit(self, event: Event) -> None:
        """Emits event handling it's scope."""

        if isinstance(event, LocalEvent):
            task = self.notify_channel(event)
        elif isinstance(event, OuterEvent):
            task = self.notify_channels(event)
        elif isinstance(event, GlobalEvent):
            task = self.notify_everyone(event)
        else:
            server_log.info(f"Emitter: unknown event type: {event}")

            return

        # TODO: use self.app.loop.ensure_future() ?
        asyncio.create_task(task)

    async def notify_channel(self, event: LocalEvent) -> None:
        """Dispatches event for all users in channel of event."""

        server_log.debug(f"Notifying channel {event.channel_id}: {event}")

        to_close = []

        async with self._lock:
            for user_id in self._channels.get(event.channel_id, ()):
                for listener in self._listeners[user_id]:
                    if not await listener.event_notify(event):
                        to_close.append(listener)

        for listener in to_close:
            await listener.close()

    async def notify_channels(self, event: OuterEvent) -> None:
        """
        Dispatches event for all users sharing channel with user of event.
        """

        server_log.debug(f"Notifying user {event.user_id} channels: {event}")

        to_close = []

        async with self._lock:
            for channel_id in self._users.get(event.user_id, ()):
                for user_id in self._channels[channel_id]:
                    for listener in self._listeners[user_id]:
                        if not await listener.event_notify(event):
                            to_close.append(listener)

        for listener in to_close:
            await listener.close()

    async def notify_everyone(self, event: GlobalEvent) -> None:
        """Discpatches event for all connected users."""

        server_log.debug(f"Notifying everyone: {event}")

        to_close = []

        async with self._lock:
            for listeners in self._listeners.values():
                for listener in listeners:
                    if not await listener.event_notify(event):
                        to_close.append(listener)

        for listener in to_close:
            await listener.close()

    async def create_listener(self, req: web.Request) -> Optional[Listener]:
        """Creates listener (websocket connection)."""

        ws = web.WebSocketResponse()

        await ws.prepare(req)

        if self._closing:
            await ws.close()

            return None

        return Listener(ws, self)

    async def add_listener(self, listener: Listener) -> None:
        """Registers listener allowing it to recieve events."""

        if listener.user_id is None:
            server_log.warn(
                f"Emitter: unable to add listener, user_id is None"
            )
            return

        channels = await self._app["pg_conn"].fetchval(
            "SELECT channel_ids FROM users WHERE id = $1", listener.user_id
        )

        async with self._lock:
            for channel_id in channels:
                if channel_id in self._channels:
                    self._channels[channel_id].add(listener.user_id)
                else:
                    self._channels[channel_id] = {listener.user_id}

            self._users[listener.user_id] = set(channels)

            if listener.user_id in self._listeners:
                self._listeners[listener.user_id].add(listener)
            else:
                self._listeners[listener.user_id] = {listener}

    async def remove_listener(self, listener: Listener) -> None:
        """Removes registered listener stopping sending events to it."""

        if listener.user_id is None:  # user did not identify
            return

        if listener.user_id not in self._listeners:  # already cleanud up
            return

        async with self._lock:
            self._listeners[listener.user_id].remove(listener)

            if self._listeners[listener.user_id]:
                return
            else:
                del self._listeners[listener.user_id]

            for channel_id in tuple(self._users[listener.user_id]):
                self._channels[channel_id].remove(listener.user_id)

                if not self._channels[channel_id]:
                    del self._channels[channel_id]

                self._users[listener.user_id].remove(channel_id)

                if not self._users[listener.user_id]:
                    del self._users[listener.user_id]

    async def close(
        self,
        app: web.Application,
        *,
        code: CloseCode = CloseCode.NORMAL,
        message: bytes = b"",
    ) -> None:
        """Stops sending events and closes all connections."""

        self._closing = True

        for listeners in self._listeners.values():
            for listener in listeners:
                await listener.close(code=code, message=message, cleanup=False)

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} closing={self._closing}>"
