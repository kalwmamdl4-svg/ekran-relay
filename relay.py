"""Visible, viewer-only remote screen sharing relay (WebSocket)."""
import asyncio
import json
import logging
import os

import websockets
from websockets.exceptions import ConnectionClosed

HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", "8080"))
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

# room -> {"hosts": {host_id: host_record}, "viewers": set(ws), "targets": {viewer_ws: host_id}}
rooms = {}


def get_room(room_name):
    return rooms.setdefault(room_name, {"hosts": {}, "viewers": set(), "targets": {}})


def host_list(room):
    return [
        {
            "id": hid,
            "name": item["name"],
            "busy": item["viewer"] is not None,
            "status": item["status"],
            "online_seconds": item["online_seconds"],
        }
        for hid, item in room["hosts"].items()
    ]


async def send_json(ws, data):
    try:
        await ws.send(json.dumps(data, ensure_ascii=False))
        return True
    except (ConnectionClosed, OSError):
        return False


async def update_lists(room):
    packet = json.dumps({"type": "host_list", "hosts": host_list(room)}, ensure_ascii=False)
    for viewer in list(room["viewers"]):
        try:
            await viewer.send(packet)
        except (ConnectionClosed, OSError):
            room["viewers"].discard(viewer)


async def detach_viewer(room, viewer):
    old_id = room["targets"].pop(viewer, None)
    if old_id:
        host = room["hosts"].get(old_id)
        if host and host["viewer"] is viewer:
            host["viewer"] = None
            await send_json(host["ws"], {"type": "peer_left"})
            await update_lists(room)


async def host_session(ws, name, hello):
    room = get_room(name)
    host_id = str(hello.get("host_id") or "").strip()
    if not host_id or len(host_id) > 100:
        await send_json(ws, {"type": "error", "message": "Geçersiz HOST kimliği."})
        return
    if host_id in room["hosts"]:
        # Never let a second unauthenticated connection evict the first one.
        await send_json(ws, {"type": "error", "message": "Bu HOST kimliği halen bağlı. Biraz sonra yeniden deneyin."})
        return

    host = {
        "ws": ws, "name": str(hello.get("name") or "Bilgisayar")[:80],
        "viewer": None, "status": "active", "online_seconds": 0,
    }
    room["hosts"][host_id] = host
    logging.info("HOST bağlandı: oda=%s id=%s", name, host_id)
    await send_json(ws, {"type": "registered", "host_id": host_id})
    await update_lists(room)
    try:
        async for packet in ws:
            # Frames are sent only to the connected viewer.
            if isinstance(packet, bytes):
                viewer = host["viewer"]
                if viewer is not None:
                    try:
                        await viewer.send(packet)
                    except (ConnectionClosed, OSError):
                        await detach_viewer(room, viewer)
                continue

            try:
                data = json.loads(packet)
            except (ValueError, TypeError):
                continue
            if not isinstance(data, dict):
                continue
            kind = data.get("type")
            if kind == "presence":
                host["status"] = str(data.get("status") or "active")[:16]
                try:
                    host["online_seconds"] = max(0, int(data.get("online_seconds") or 0))
                except (TypeError, ValueError):
                    pass
                await update_lists(room)
            elif kind in ("accepted", "stopped"):
                viewer = host["viewer"]
                if viewer is not None:
                    await send_json(viewer, {"type": kind, "host_id": host_id, "name": host["name"]})
    except ConnectionClosed:
        pass
    finally:
        if room["hosts"].get(host_id) is host:
            del room["hosts"][host_id]
            if host["viewer"] is not None:
                viewer = host["viewer"]
                room["targets"].pop(viewer, None)
                await send_json(viewer, {"type": "peer_left"})
            await update_lists(room)
        logging.info("HOST ayrıldı: oda=%s id=%s", name, host_id)


async def viewer_session(ws, name):
    room = get_room(name)
    room["viewers"].add(ws)
    await send_json(ws, {"type": "host_list", "hosts": host_list(room)})
    logging.info("VIEWER bağlandı: oda=%s", name)
    try:
        async for packet in ws:
            if not isinstance(packet, str):
                continue
            try:
                data = json.loads(packet)
            except (ValueError, TypeError):
                continue
            if not isinstance(data, dict):
                continue
            kind = data.get("type")
            if kind == "connect_host":
                host_id = str(data.get("host_id") or "")
                host = room["hosts"].get(host_id)
                if host is None:
                    await send_json(ws, {"type": "error", "message": "Bilgisayar artık çevrimiçi değil."})
                    continue
                if host["viewer"] is not None and host["viewer"] is not ws:
                    await send_json(ws, {"type": "host_busy", "message": "Bilgisayara başka bir izleyici bağlı."})
                    continue
                if room["targets"].get(ws) != host_id:
                    await detach_viewer(room, ws)
                    host["viewer"] = ws
                    room["targets"][ws] = host_id
                    await send_json(ws, {"type": "connecting", "name": host["name"], "host_id": host_id})
                    if not await send_json(host["ws"], {"type": "viewer_joined"}):
                        await detach_viewer(room, ws)
                        await send_json(ws, {"type": "error", "message": "Bilgisayara ulaşılamıyor."})
                    await update_lists(room)
            elif kind == "disconnect_host":
                await detach_viewer(room, ws)
                await send_json(ws, {"type": "peer_left"})
            elif kind == "ping":
                await send_json(ws, {"type": "pong", "t": data.get("t")})
    except ConnectionClosed:
        pass
    finally:
        await detach_viewer(room, ws)
        room["viewers"].discard(ws)
        logging.info("VIEWER ayrıldı: oda=%s", name)


async def handler(ws):
    try:
        raw = await asyncio.wait_for(ws.recv(), timeout=15)
        if not isinstance(raw, str):
            return
        hello = json.loads(raw)
        if not isinstance(hello, dict):
            return
        name = hello.get("room")
        if not isinstance(name, str) or not (12 <= len(name) <= 128):
            await send_json(ws, {"type": "error", "message": "Geçersiz oda kodu. HOST ekranındaki kodu girin."})
            return
        role = hello.get("role")
        if role == "host":
            await host_session(ws, name, hello)
        elif role == "viewer":
            await viewer_session(ws, name)
        else:
            await send_json(ws, {"type": "error", "message": "Geçersiz bağlantı rolü."})
    except (ConnectionClosed, asyncio.TimeoutError, ValueError, TypeError):
        return
    except Exception:
        logging.exception("Bağlantı işlenirken hata")


async def main():
    logging.info("Ekran paylaşım relay: %s:%s", HOST, PORT)
    async with websockets.serve(
        handler, HOST, PORT, max_size=16 * 1024 * 1024,
        ping_interval=20, ping_timeout=30, close_timeout=5,
    ):
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
