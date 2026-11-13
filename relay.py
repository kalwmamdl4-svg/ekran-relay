"""
relay.py — Aracı (relay) sunucu. Render gibi herkese açık bir yerde çalışır.

MANTIK:
  İki ev bilgisayarı da modem arkasındadır (NAT) — dışarıdan bağlantı kabul edemez.
  AMA ikisi de DIŞARI bağlantı kurabilir (internete çıkabildikleri gibi).
  Bu sunucunun herkesin erişebildiği bir adresi var. Host (paylaşan) ve viewer
  (izleyen) İKİSİ de buraya dışarı doğru bağlanır. Sunucu, birinden geleni
  diğerine kopyalar — santral memuru gibi.

  "Oda kodu" = hangi iki bağlantının eşleşeceğini belirler.
  Host "oyun123 odasındayım" der, viewer "oyun123 odasındayım" der → eşleşirler.
  (TeamViewer'daki "ID" aslında böyle bir oda kodudur, sunucuları da bu relay'dir.)

TAŞIMA: WebSocket kullanıyoruz (wss, 443 portu). Çünkü Render gibi servisler
sana rastgele TCP portu açmaz, sadece HTTPS/WebSocket yönlendirir. 443 portu da
neredeyse her yerde (okul, iş, ev) açıktır — bu yüzden çok sağlam.
"""

import asyncio
import http
import json
import os

import websockets

odalar = {}   # oda_kodu -> {"host": ws, "viewer": ws}


async def saglik_kontrolu(path, request_headers):
    """Render ve tarayıcılar '/' adresine normal HTTP atar (WebSocket değil).
    Sadece '/ws' yolunu WebSocket kabul et; gerisine 200 OK dön ki Render
    servisi 'ayakta' saysın."""
    if path != "/ws":
        return http.HTTPStatus.OK, [("Content-Type", "text/plain")], b"relay calisiyor\n"
    return None   # /ws ise WebSocket el sıkışmasına devam et


def karsi_taraf(oda, rol):
    r = odalar.get(oda, {})
    return r.get("viewer" if rol == "host" else "host")


async def handler(ws):
    rol = None
    oda = None
    try:
        # İlk mesaj kayıt olmalı: {"type":"register","role":"host|viewer","room":"..."}
        ilk = await ws.recv()
        bilgi = json.loads(ilk)
        rol = bilgi["role"]
        oda = bilgi["room"]

        r = odalar.setdefault(oda, {})
        if rol in r:
            await ws.send(json.dumps({"type": "error", "reason": "bu rol dolu"}))
            return
        r[rol] = ws

        if "host" in r and "viewer" in r:
            # ikisi de hazır -> host'a "izleyici geldi" haberini ver
            await r["host"].send(json.dumps({"type": "viewer_joined"}))
        elif rol == "viewer":
            # viewer geldi ama host yok -> bekle
            await ws.send(json.dumps({"type": "no_host"}))

        # Bundan sonrası: bu bağlantıdan geleni karşı tarafa aktar (host <-> viewer)
        async for mesaj in ws:
            hedef = karsi_taraf(oda, rol)
            if hedef is not None:
                # str ise metin frame, bytes ise ikili frame olarak gider
                await hedef.send(mesaj)
    except Exception:
        pass
    finally:
        # Temizlik: bu bağlantıyı odadan çıkar, karşı tarafa haber ver
        r = odalar.get(oda)
        if r and r.get(rol) is ws:
            del r[rol]
            hedef = r.get("viewer" if rol == "host" else "host")
            if hedef is not None:
                try:
                    await hedef.send(json.dumps({"type": "peer_left"}))
                except Exception:
                    pass
            if not r:
                odalar.pop(oda, None)


async def main():
    # Render, dinlenecek portu PORT ortam değişkeninde verir.
    port = int(os.environ.get("PORT", 8080))
    async with websockets.serve(handler, "0.0.0.0", port,
                                process_request=saglik_kontrolu):
        print(f"Relay {port} portunda calisiyor")
        await asyncio.Future()   # sonsuza kadar çalış


if __name__ == "__main__":
    asyncio.run(main())
