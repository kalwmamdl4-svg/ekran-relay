"""Authenticated, consent-gated WebSocket relay. Set RELAY_TOKEN before starting."""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from http import HTTPStatus
import logging
import os
import secrets
import time
import uuid

from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from common import VERSION, MAX_MESSAGE, FRAME, UPLOAD, DOWNLOAD, FILE_TYPES, decode, encode, unpack

LOG = logging.getLogger("relay")


class AsyncOutbox:
    def __init__(self):
        self.cv = asyncio.Condition()
        self.control, self.bulk = deque(), deque()
        self.frame = None
        self.closed = False
        self.turn = 0

    async def put(self, value, lane="control"):
        async with self.cv:
            if self.closed:
                raise ConnectionError("closed outbox")
            if lane == "frame":
                self.frame = value
            else:
                target, limit = (self.bulk, 16) if lane == "bulk" else (self.control, 128)
                await asyncio.wait_for(self.cv.wait_for(lambda: len(target) < limit or self.closed), 5)
                if self.closed:
                    raise ConnectionError("closed outbox")
                target.append(value)
            self.cv.notify_all()

    async def get(self):
        async with self.cv:
            await self.cv.wait_for(lambda: self.closed or self.control or self.bulk or self.frame is not None)
            if self.closed:
                return None
            if self.frame is not None and self.turn % 4 == 3:
                value, self.frame = self.frame, None
            elif self.control and (self.turn % 4 != 2 or not self.bulk):
                value = self.control.popleft()
            elif self.bulk:
                value = self.bulk.popleft()
            elif self.frame is not None:
                value, self.frame = self.frame, None
            else:
                value = self.control.popleft()
            self.turn += 1
            self.cv.notify_all()
            return value

    async def close(self):
        async with self.cv:
            self.closed = True
            self.control.clear()
            self.bulk.clear()
            self.frame = None
            self.cv.notify_all()


@dataclass(eq=False)
class Peer:
    ws: object
    role: str
    room: str
    name: str
    host_id: str = ""
    peer: object = None
    session: str = ""
    accepted: bool = False
    since: float = field(default_factory=time.monotonic)
    out: AsyncOutbox = field(default_factory=AsyncOutbox)
    pending_timer: object = None


class Relay:
    def __init__(self, token):
        if not isinstance(token, str) or len(token) < 20:
            raise ValueError("RELAY_TOKEN en az 20 karakter olmalı; secrets.token_urlsafe(32) kullanın.")
        self.token = token
        self.hosts = {}
        self.peers = set()

    async def send(self, peer, data, lane=None):
        if isinstance(data, dict):
            lane = lane or ("bulk" if data["type"] in FILE_TYPES else "control")
            data = encode(data)
        try:
            await peer.out.put(data, lane or "control")
        except (TimeoutError, ConnectionError):
            await peer.ws.close(code=1013, reason="Alıcı çok yavaş")

    async def writer(self, peer):
        try:
            while (data := await peer.out.get()) is not None:
                await peer.ws.send(data)
        except ConnectionClosed:
            pass
        except Exception:
            LOG.exception("Relay writer failed")
            await peer.ws.close(code=1011, reason="Gönderim hatası")

    async def host_list(self, room):
        hosts = [{"id": p.host_id, "name": p.name, "busy": p.peer is not None,
                  "online_seconds": int(time.monotonic() - p.since)}
                 for (r, _), p in self.hosts.items() if r == room]
        await asyncio.gather(*(self.send(p, {"type": "host_list", "hosts": hosts})
                               for p in list(self.peers) if p.role == "viewer" and p.room == room))

    async def detach(self, peer, reason):
        other = peer.peer
        session = peer.session
        for item in (peer, other):
            if item is not None:
                item.peer, item.session, item.accepted = None, "", False
                if item.pending_timer:
                    item.pending_timer.cancel()
                    item.pending_timer = None
                item.out.frame = None
        for item in (peer, other):
            if item is not None:
                await self.send(item, {"type": "session_ended", "session_id": session, "reason": reason})
        await self.host_list(peer.room)

    async def request(self, viewer, host_id):
        host = self.hosts.get((viewer.room, host_id))
        if not host:
            return await self.send(viewer, {"type": "error", "message": "Bilgisayar çevrimiçi değil"})
        if host.peer is not None:
            return await self.send(viewer, {"type": "error", "message": "Bilgisayar meşgul"})
        if viewer.peer:
            await self.detach(viewer, "viewer_switched")
            if host.peer is not None:
                return await self.send(viewer, {"type": "error", "message": "Bilgisayar meşgul"})
        # Reserve both peers before awaiting any network-related work.
        session = uuid.uuid4().hex
        viewer.peer, host.peer = host, viewer
        viewer.session = host.session = session
        loop = asyncio.get_running_loop()
        host.pending_timer = loop.call_later(60, lambda: asyncio.create_task(self.expire(host, session)))
        await self.send(viewer, {"type": "connecting", "session_id": session, "host_id": host.host_id})
        await self.send(host, {"type": "session_request", "session_id": session, "viewer_name": viewer.name})
        await self.host_list(host.room)

    async def expire(self, host, session):
        if host.session == session and not host.accepted:
            await self.detach(host, "approval_timeout")

    async def route(self, peer, value):
        if isinstance(value, bytes):
            tag, session, _, _ = unpack(value)
            if peer.peer and peer.accepted and session == peer.session:
                allowed = (FRAME, DOWNLOAD) if peer.role == "host" else (UPLOAD,)
                if tag not in allowed:
                    raise ValueError("Yanlış binary yönü")
                await self.send(peer.peer, value, "frame" if tag == FRAME else "bulk")
            return
        data = decode(value)
        kind = data["type"]
        if kind == "ping":
            return await self.send(peer, {"type": "pong", "id": data.get("id")})
        if peer.role == "viewer" and kind == "list_hosts":
            return await self.host_list(peer.room)
        if peer.role == "viewer" and kind == "connect_host":
            return await self.request(peer, str(data.get("host_id", "")))
        if kind == "disconnect_host":
            if peer.peer:
                await self.detach(peer, "host_stopped" if peer.role == "host" else "viewer_stopped")
            return
        if not peer.peer or data.get("session_id") != peer.session:
            return
        if peer.role == "host" and kind in ("session_accept", "session_reject"):
            if kind == "session_reject":
                return await self.detach(peer, "host_declined")
            if peer.accepted:
                return
            peer.accepted = peer.peer.accepted = True
            if peer.pending_timer:
                peer.pending_timer.cancel()
                peer.pending_timer = None
            accepted = {"type": "accepted", "session_id": peer.session, "host_id": peer.host_id,
                        "name": peer.name, "monitors": data.get("monitors", []),
                        "permissions": data.get("permissions", {}), "shared_folder": data.get("shared_folder", "")}
            return await self.send(peer.peer, accepted)
        allowed = ({"ctrl", "quality", "monitor", "fs_list", "file_request", "host_ping", *FILE_TYPES}
                   if peer.role == "viewer" else {"host_pong", "monitor_changed", "fs_list_result", "stream_stats", "error", *FILE_TYPES})
        if peer.accepted and kind in allowed:
            await self.send(peer.peer, data)

    async def handler(self, ws):
        peer, writer = None, None
        try:
            if len(self.peers) >= 128:
                return await ws.close(code=1013, reason="Kapasite dolu")
            raw = await asyncio.wait_for(ws.recv(), timeout=15)
            data = decode(raw)
            token = data.get("token", "")
            if data.get("type") != "register" or data.get("version") != VERSION:
                return await ws.close(code=4003, reason="Tüm dosyaları birlikte v3 sürümüne güncelleyin")
            if not isinstance(token, str) or not secrets.compare_digest(token.encode(), self.token.encode()):
                return await ws.close(code=4003, reason="Bağlantı anahtarı yanlış")
            role, room = data.get("role"), data.get("room")
            if role not in ("host", "viewer") or not isinstance(room, str) or not 1 <= len(room) <= 64:
                raise ValueError("Geçersiz rol/oda")
            host_id = str(data.get("host_id", ""))
            if role == "host":
                uuid.UUID(host_id)
                if (room, host_id) in self.hosts:
                    return await ws.close(code=4001, reason="Aynı host zaten bağlı; diğer kopyayı kapatın")
            peer = Peer(ws, role, room, str(data.get("name", role))[:80], host_id)
            self.peers.add(peer)
            if role == "host":
                self.hosts[(room, host_id)] = peer
            writer = asyncio.create_task(self.writer(peer))
            await self.send(peer, {"type": "registered", "version": VERSION, "host_id": host_id})
            await self.host_list(room)
            LOG.info("Bağlandı role=%s id=%s room=%s", role, host_id, room)
            async for value in ws:
                await self.route(peer, value)
        except ConnectionClosed as error:
            LOG.info("Bağlantı kapandı: %s", error)
        except (ValueError, KeyError, TypeError, TimeoutError):
            LOG.warning("Geçersiz protokol mesajı", exc_info=True)
            await ws.close(code=1008, reason="Geçersiz protokol mesajı")
        except Exception:
            LOG.exception("Relay hatası")
            await ws.close(code=1011, reason="Relay hatası")
        finally:
            if peer:
                self.peers.discard(peer)
                if self.hosts.get((peer.room, peer.host_id)) is peer:
                    self.hosts.pop((peer.room, peer.host_id))
                if peer.peer:
                    await self.detach(peer, "peer_offline")
                else:
                    await self.host_list(peer.room)
                await peer.out.close()
            if writer:
                writer.cancel()
                await asyncio.gather(writer, return_exceptions=True)


def health(connection, request):
    if request.path == "/health":
        return connection.respond(HTTPStatus.OK, "ok\n")


async def main():
    relay = Relay(os.environ.get("RELAY_TOKEN", ""))
    async with serve(relay.handler, os.environ.get("HOST", "0.0.0.0"), int(os.environ.get("PORT", "8080")),
                     max_size=MAX_MESSAGE, max_queue=16, write_limit=65536,
                     ping_interval=15, ping_timeout=20, close_timeout=3, compression=None,
                     process_request=health):
        LOG.info("Relay hazır; health=/health")
        await asyncio.Future()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

