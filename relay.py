
import asyncio
import json
import os
import uuid
import logging

import websockets
from websockets.exceptions import ConnectionClosed

HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", "8080"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

odalar = {}


def oda_al(oda_adi):
    return odalar.setdefault(
        oda_adi,
        {"hosts": {}, "viewers": set()}
    )


def durum_hesapla(idle_seconds):
    if idle_seconds < 60:
        return "active"
    if idle_seconds < 300:
        return "idle"
    return "afk"


def host_listesi_hazirla(oda_adi):
    oda = odalar.get(oda_adi)
    if not oda:
        return []

    return [
        {
            "id": host_id,
            "name": host["name"],
            "online_seconds": host["online_seconds"],
            "idle_seconds": host["idle_seconds"],
            "status": host["status"],
            "screen": host["screen"],
            "busy": host["viewer"] is not None,
        }
        for host_id, host in oda["hosts"].items()
    ]


async def mesaj_gonder(ws, veri):
    try:
        await ws.send(json.dumps(veri))
        return True
    except ConnectionClosed:
        return False
    except Exception as e:
        logging.warning("Mesaj gönderilemedi: %s", e)
        return False


async def host_listesi_gonder(oda_adi):
    oda = odalar.get(oda_adi)
    if not oda:
        return

    veri = {
        "type": "host_list",
        "hosts": host_listesi_hazirla(oda_adi)
    }

    for viewer in list(oda["viewers"]):
        if not await mesaj_gonder(viewer, veri):
            oda["viewers"].discard(viewer)


async def hosta_baglan(viewer_ws, oda_adi, host_id):
    oda = odalar.get(oda_adi)
    if not oda:
        await mesaj_gonder(viewer_ws, {
            "type": "error",
            "message": "Oda bulunamadı."
        })
        return None

    host = oda["hosts"].get(host_id)

    if not host:
        await mesaj_gonder(viewer_ws, {
            "type": "error",
            "message": "Bilgisayar çevrimiçi değil."
        })
        return None

    if (
        host["viewer"] is not None
        and host["viewer"] is not viewer_ws
    ):
        await mesaj_gonder(viewer_ws, {
            "type": "host_busy",
            "host_id": host_id,
            "message": "Bilgisayara başka biri bağlı."
        })
        return None

    # Önceki bilgisayar bağlantısını bırak.
    for hid, h in list(oda["hosts"].items()):
        if hid != host_id and h["viewer"] is viewer_ws:
            h["viewer"] = None
            await mesaj_gonder(
                h["ws"], {"type": "peer_left"}
            )

    zaten_bagli = host["viewer"] is viewer_ws
    host["viewer"] = viewer_ws

    await mesaj_gonder(viewer_ws, {
        "type": "connecting",
        "host_id": host_id,
        "name": host["name"]
    })

    if not zaten_bagli:
        await mesaj_gonder(host["ws"], {
            "type": "viewer_joined"
        })

    await host_listesi_gonder(oda_adi)
    return host_id


async def host_handler(ws, oda_adi, bilgi):
    oda = oda_al(oda_adi)

    host_id = bilgi.get("host_id")
    if not isinstance(host_id, str) or not host_id.strip():
        host_id = str(uuid.uuid4())

    eski_host = oda["hosts"].get(host_id)
    eski_viewer = None

    if eski_host:
        eski_ws = eski_host["ws"]

        if eski_ws is not ws:
            logging.warning(
                "Aynı HOST ID yeniden bağlandı: %s",
                host_id
            )

            eski_viewer = eski_host.get("viewer")

            # Eski bağlantının mesaj göndermesini durdur.
            try:
                await asyncio.wait_for(
                    eski_ws.close(
                        code=4001,
                        reason="Yeni HOST bağlantısı açıldı"
                    ),
                    timeout=3
                )
            except Exception:
                pass

    host = {
        "ws": ws,
        "name": str(
            bilgi.get("name") or "Bilinmeyen PC"
        ),
        "room": oda_adi,
        "online_seconds": 0,
        "idle_seconds": 0,
        "status": "active",
        "screen": None,
        "viewer": eski_viewer,
    }

    oda["hosts"][host_id] = host

    logging.info(
        "[HOST] Bağlandı | Oda=%s | ID=%s | Ad=%s",
        oda_adi, host_id, host["name"]
    )

    await mesaj_gonder(ws, {
        "type": "registered",
        "host_id": host_id,
        "room": oda_adi
    })

    if eski_viewer is not None:
        await mesaj_gonder(eski_viewer, {
            "type": "peer_left"
        })
        host["viewer"] = None

    await host_listesi_gonder(oda_adi)

    try:
        async for mesaj in ws:

            # Eski bağlantı artık veri gönderemez.
            if oda["hosts"].get(host_id) is not host:
                break

            if isinstance(mesaj, str):
                try:
                    veri = json.loads(mesaj)
                except json.JSONDecodeError:
                    veri = None

                if isinstance(veri, dict):
                    if veri.get("type") == "presence":
                        host["online_seconds"] = int(
                            veri.get("online_seconds") or 0
                        )
                        host["idle_seconds"] = int(
                            veri.get("idle_seconds") or 0
                        )
                        host["status"] = durum_hesapla(
                            host["idle_seconds"]
                        )
                        host["screen"] = veri.get("screen")

                        await host_listesi_gonder(oda_adi)
                        continue

            viewer = host.get("viewer")

            if viewer is not None:
                try:
                    await viewer.send(mesaj)
                except ConnectionClosed:
                    if host["viewer"] is viewer:
                        host["viewer"] = None
                    await host_listesi_gonder(oda_adi)

    except ConnectionClosed as e:
        logging.warning(
            "[HOST] Bağlantı kesildi | ID=%s | Kod=%s",
            host_id, e.code
        )

    except Exception:
        logging.exception("[HOST] Beklenmeyen hata")

    finally:
        # KRİTİK: Eski bağlantı yeni HOST'u silemez.
        if oda["hosts"].get(host_id) is host:
            oda["hosts"].pop(host_id, None)

            viewer = host.get("viewer")
            if viewer is not None:
                await mesaj_gonder(viewer, {
                    "type": "peer_left"
                })

            await host_listesi_gonder(oda_adi)

            logging.info(
                "[HOST] Ayrıldı | ID=%s", host_id
            )


async def viewer_handler(ws, oda_adi):
    oda = oda_al(oda_adi)
    oda["viewers"].add(ws)

    hedef_host_id = None

    await mesaj_gonder(ws, {
        "type": "host_list",
        "hosts": host_listesi_hazirla(oda_adi)
    })

    logging.info("[VIEWER] Bağlandı | Oda=%s", oda_adi)

    try:
        async for mesaj in ws:

            if isinstance(mesaj, str):
                try:
                    veri = json.loads(mesaj)
                except json.JSONDecodeError:
                    veri = None

                if isinstance(veri, dict):
                    mesaj_tipi = veri.get("type")

                    if mesaj_tipi == "connect_host":
                        yeni_id = veri.get("host_id")
                        if not yeni_id:
                            continue

                        yeni_hedef = await hosta_baglan(
                            ws, oda_adi, yeni_id
                        )

                        if yeni_hedef:
                            hedef_host_id = yeni_hedef

                        continue

                    if mesaj_tipi == "disconnect_host":
                        if hedef_host_id:
                            host = oda["hosts"].get(
                                hedef_host_id
                            )

                            if host and host["viewer"] is ws:
                                host["viewer"] = None
                                await mesaj_gonder(
                                    host["ws"],
                                    {"type": "peer_left"}
                                )

                        hedef_host_id = None
                        await host_listesi_gonder(oda_adi)
                        continue

            if hedef_host_id:
                host = oda["hosts"].get(hedef_host_id)

                if not host or host["viewer"] is not ws:
                    hedef_host_id = None
                    continue

                try:
                    await host["ws"].send(mesaj)
                except ConnectionClosed:
                    if host["viewer"] is ws:
                        host["viewer"] = None
                    hedef_host_id = None

    except ConnectionClosed:
        pass

    except Exception:
        logging.exception("[VIEWER] Hata")

    finally:
        # Viewer'ın bağlı olduğu tüm HOST kayıtlarını temizle.
        for host in list(oda["hosts"].values()):
            if host["viewer"] is ws:
                host["viewer"] = None
                await mesaj_gonder(
                    host["ws"],
                    {"type": "peer_left"}
                )

        oda["viewers"].discard(ws)

        await host_listesi_gonder(oda_adi)
        logging.info("[VIEWER] Ayrıldı | Oda=%s", oda_adi)


async def handler(ws):
    try:
        ilk_mesaj = await asyncio.wait_for(
            ws.recv(),
            timeout=15
        )

        if not isinstance(ilk_mesaj, str):
            await ws.close(code=1003)
            return

        try:
            bilgi = json.loads(ilk_mesaj)
        except json.JSONDecodeError:
            await mesaj_gonder(ws, {
                "type": "error",
                "message": "Geçersiz JSON."
            })
            return

        if not isinstance(bilgi, dict):
            return

        role = bilgi.get("role")
        oda_adi = bilgi.get("room") or "oyun123"

        if not isinstance(oda_adi, str):
            return

        if role == "host":
            await host_handler(ws, oda_adi, bilgi)

        elif role == "viewer":
            await viewer_handler(ws, oda_adi)

        else:
            await mesaj_gonder(ws, {
                "type": "error",
                "message": "Geçersiz rol."
            })

    except asyncio.TimeoutError:
        logging.warning("İlk mesaj zaman aşımı")

    except ConnectionClosed:
        pass

    except Exception:
        logging.exception("[HANDLER] Hata")


async def main():
    logging.info("================================")
    logging.info(" EKRAN PAYLASIM RELAY")
    logging.info(" Port: %s", PORT)
    logging.info("================================")

    async with websockets.serve(
        handler,
        HOST,
        PORT,
        max_size=None,
        ping_interval=20,
        ping_timeout=30,
        close_timeout=5
    ):
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
