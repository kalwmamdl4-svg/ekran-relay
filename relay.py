import asyncio
import json
import os

import websockets


# =========================================================
# AYARLAR
# =========================================================

HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", "8080"))


# =========================================================
# VERİLER
# =========================================================

odalar = {}

# Her oda:
#
# {
#     "hosts": {
#         "abc123": {
#             "ws": websocket,
#             "name": "AHMET-PC",
#             "room": "oyun123",
#             "online_seconds": 0,
#             "idle_seconds": 0,
#             "status": "active",
#             "viewer": websocket veya None
#         }
#     },
#
#     "viewers": set()
# }


def oda_al(oda_adi):
    if oda_adi not in odalar:
        odalar[oda_adi] = {
            "hosts": {},
            "viewers": set(),
        }

    return odalar[oda_adi]


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

    liste = []

    for host_id, host in oda["hosts"].items():
        liste.append({
            "id": host_id,
            "name": host.get("name", "Bilinmeyen PC"),
            "online_seconds": int(host.get("online_seconds", 0)),
            "idle_seconds": int(host.get("idle_seconds", 0)),
            "status": host.get("status", "active"),
            "screen": host.get("screen"),
            "busy": host.get("viewer") is not None,
        })

    return liste


async def host_listesi_gonder(oda_adi):
    oda = odalar.get(oda_adi)

    if not oda:
        return

    mesaj = json.dumps({
        "type": "host_list",
        "hosts": host_listesi_hazirla(oda_adi)
    })

    silinecekler = []

    for viewer in list(oda["viewers"]):
        try:
            await viewer.send(mesaj)
        except Exception:
            silinecekler.append(viewer)

    for viewer in silinecekler:
        oda["viewers"].discard(viewer)


async def mesaj_gonder(ws, veri):
    try:
        await ws.send(json.dumps(veri))
        return True
    except Exception:
        return False


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
            "message": "Bu bilgisayar artık çevrimiçi değil."
        })

        await host_listesi_gonder(oda_adi)
        return None

    eski_host_id = None

    # Viewer daha önce başka PC'ye bağlıysa bağlantıyı bırak
    for hid, h in oda["hosts"].items():
        if h.get("viewer") is viewer_ws:
            eski_host_id = hid
            break

    if eski_host_id and eski_host_id != host_id:
        eski_host = oda["hosts"].get(eski_host_id)

        if eski_host:
            eski_host["viewer"] = None

            await mesaj_gonder(eski_host["ws"], {
                "type": "peer_left"
            })

    # Bu PC başka bir viewer tarafından kullanılıyorsa
    if host.get("viewer") is not None and host["viewer"] is not viewer_ws:
        await mesaj_gonder(viewer_ws, {
            "type": "host_busy",
            "host_id": host_id,
            "message": "Bu bilgisayara başka biri bağlı."
        })

        return None

    host["viewer"] = viewer_ws

    await mesaj_gonder(viewer_ws, {
        "type": "connecting",
        "host_id": host_id,
        "name": host["name"]
    })

    await mesaj_gonder(host["ws"], {
        "type": "viewer_joined"
    })

    return host_id


async def host_handler(ws, oda_adi, bilgi):
    oda = oda_al(oda_adi)

    host_id = bilgi.get("host_id")

    if not host_id:
        host_id = "host-" + str(id(ws))

    if host_id in oda["hosts"]:
        await mesaj_gonder(ws, {
            "type": "error",
            "message": "Bu host kimliği zaten kullanımda."
        })
        return

    host = {
        "ws": ws,
        "name": bilgi.get("name", "Bilinmeyen PC"),
        "room": oda_adi,
        "online_seconds": 0,
        "idle_seconds": 0,
        "status": "active",
        "screen": None,
        "viewer": None,
    }

    oda["hosts"][host_id] = host

    print(
        f"[HOST] {host['name']} bağlandı | "
        f"Oda: {oda_adi} | ID: {host_id}"
    )

    await host_listesi_gonder(oda_adi)

    try:
        async for mesaj in ws:

            # Host -> Relay mesajları
            if isinstance(mesaj, str):

                try:
                    veri = json.loads(mesaj)
                except Exception:
                    veri = {}

                mesaj_tipi = veri.get("type")

                # Presence / durum güncellemesi
                if mesaj_tipi == "presence":

                    host["online_seconds"] = int(
                        veri.get("online_seconds", 0)
                    )

                    host["idle_seconds"] = int(
                        veri.get("idle_seconds", 0)
                    )

                    host["status"] = durum_hesapla(
                        host["idle_seconds"]
                    )

                    host["screen"] = veri.get("screen")

                    await host_listesi_gonder(oda_adi)

                    continue

            # Host'tan gelen diğer mesajları viewer'a gönder
            viewer = host.get("viewer")

            if viewer is not None:

                try:
                    await viewer.send(mesaj)

                except Exception:
                    host["viewer"] = None

    except Exception as e:
        print(f"[HOST HATA] {host['name']}: {e}")

    finally:

        viewer = host.get("viewer")

        if viewer is not None:
            await mesaj_gonder(viewer, {
                "type": "peer_left"
            })

        oda["hosts"].pop(host_id, None)

        print(
            f"[HOST] {host['name']} ayrıldı | "
            f"Oda: {oda_adi}"
        )

        await host_listesi_gonder(oda_adi)


async def viewer_handler(ws, oda_adi):
    oda = oda_al(oda_adi)

    oda["viewers"].add(ws)

    hedef_host_id = None

    await host_listesi_gonder(oda_adi)

    # İlk listeyi sadece bu viewer'a gönder
    await mesaj_gonder(ws, {
        "type": "host_list",
        "hosts": host_listesi_hazirla(oda_adi)
    })

    print(f"[VIEWER] Bağlandı | Oda: {oda_adi}")

    try:

        async for mesaj in ws:

            if isinstance(mesaj, str):

                try:
                    veri = json.loads(mesaj)
                except Exception:
                    veri = {}

                mesaj_tipi = veri.get("type")

                # -------------------------------------------------
                # Belirli bir hosta bağlan
                # -------------------------------------------------

                if mesaj_tipi == "connect_host":

                    yeni_host_id = veri.get("host_id")

                    if not yeni_host_id:
                        continue

                    hedef_host_id = await hosta_baglan(
                        ws,
                        oda_adi,
                        yeni_host_id
                    )

                    continue

                # -------------------------------------------------
                # Bağlantıyı kes
                # -------------------------------------------------

                if mesaj_tipi == "disconnect_host":

                    if hedef_host_id:
                        host = oda["hosts"].get(
                            hedef_host_id
                        )

                        if host and host.get("viewer") is ws:
                            host["viewer"] = None

                            await mesaj_gonder(
                                host["ws"],
                                {
                                    "type": "peer_left"
                                }
                            )

                        hedef_host_id = None

                    continue

            # Viewer mesajını hedef hosta gönder
            if hedef_host_id:

                host = oda["hosts"].get(
                    hedef_host_id
                )

                if host:

                    try:
                        await host["ws"].send(mesaj)

                    except Exception:
                        host["viewer"] = None
                        hedef_host_id = None

    except Exception as e:
        print(f"[VIEWER HATA] {e}")

    finally:

        if hedef_host_id:

            host = oda["hosts"].get(
                hedef_host_id
            )

            if host and host.get("viewer") is ws:
                host["viewer"] = None

                await mesaj_gonder(
                    host["ws"],
                    {
                        "type": "peer_left"
                    }
                )

        oda["viewers"].discard(ws)

        print(f"[VIEWER] Ayrıldı | Oda: {oda_adi}")


async def handler(ws):
    try:

        ilk_mesaj = await ws.recv()

        if not isinstance(ilk_mesaj, str):
            return

        bilgi = json.loads(ilk_mesaj)

        role = bilgi.get("role")
        oda_adi = bilgi.get("room", "oyun123")

        if role == "host":

            await host_handler(
                ws,
                oda_adi,
                bilgi
            )

        elif role == "viewer":

            await viewer_handler(
                ws,
                oda_adi
            )

        else:

            await mesaj_gonder(ws, {
                "type": "error",
                "message": "Geçersiz rol."
            })

    except Exception as e:
        print(f"[HANDLER HATA] {e}")


async def main():

    print("================================")
    print(" EKRAN PAYLASIM RELAY")
    print("================================")
    print(f"Port: {PORT}")

    async with websockets.serve(
        handler,
        HOST,
        PORT,
        max_size=None,
        ping_interval=20,
        ping_timeout=20
    ):
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
