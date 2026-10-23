"""Taskbar Island · desain J (Deck minimal, Mist Blue).

Kartu Spotify (dengan lirik), YouTube, dan Claude usage (dengan launcher) yang menempel di taskbar Windows 11.
Jalankan : pythonw island.py        (klik kanan kartu -> Keluar)
Cek logika: python island.py --selftest
"""
import asyncio
import bisect
import ctypes
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from ctypes import wintypes
from datetime import datetime, timezone
from pathlib import Path

from PyQt5.QtCore import QEasingCurve, QFileInfo, QObject, QPointF, QRectF, Qt, QTimer, QVariantAnimation, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QFontDatabase, QFontMetrics, QPainter, QPainterPath, QPen, QPixmap
from PyQt5.QtWidgets import QApplication, QFileIconProvider, QMenu, QWidget

# --- pengaturan -------------------------------------------------------------------
X_OFFSET = 190                     # jarak dari kiri layar; perbesar kalau menabrak widget cuaca
PILL_W, PILL_H = 270, 36           # kartu kecil di taskbar
CARD_W, PEEK, GAP = 320, 14, 12    # lebar kartu besar, tumpukan di belakang, jarak ke taskbar
H_SHORT, H_TALL = 196, 230        # tinggi kartu: biasa / dengan lirik atau launcher

LOCAL, ROAMING = os.environ["LOCALAPPDATA"], os.environ["APPDATA"]
APP_ICONS = {  # ikon di header kartu
    "spotify": ROAMING + r"\Spotify\Spotify.exe",
    "youtube": r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
}
LAUNCHERS = [  # (label, Start menu AppID, file ikon; "appx:<paket>|<exe>" untuk aplikasi Microsoft Store)
    ("Terminal", "Microsoft.WindowsTerminal_8wekyb3d8bbwe!App", "appx:Microsoft.WindowsTerminal|WindowsTerminal.exe"),
    ("Termius", "com.termius.mac", LOCAL + r"\Programs\Termius\Termius.exe"),
    ("VS Code", "Microsoft.VisualStudioCode", LOCAL + r"\Programs\Microsoft VS Code\Code.exe"),
    ("Discord", "com.squirrel.Discord.Discord", LOCAL + r"\Discord\app.ico"),
]

STATUS_FILE = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "island-status.json"
LRCLIB = "https://lrclib.net/api"
BROWSERS = ("brave", "chrome", "msedge", "firefox", "opera", "vivaldi")
PLAYING = 4

C = {  # Mist Blue
    "bg": "#18202c", "border": "#253044", "text": "#e6edf7", "muted": "#9badc6", "accent": "#8fb8ff",
    "track": "#253144", "art": "#2b3a55", "pip_off": "#33415a", "on_accent": "#10141b", "tile": "#1f2938",
    "back1": "#151d2a", "back1_border": "#212b3c", "back2": "#121925", "back2_border": "#1d2636",
    "claude": "#d97757",
}
FONT = "Plus Jakarta Sans"        # file di folder fonts/; huruf yang tidak ada (mis. kanji) otomatis pakai font sistem
ICON_FONT = "Segoe Fluent Icons"
GLYPH = {"prev": "\ue892", "next": "\ue893", "play": "\ue768", "pause": "\ue769", "left": "\ue76b", "right": "\ue76c"}
COMMANDS = {"prev": "try_skip_previous_async", "play": "try_toggle_play_pause_async", "next": "try_skip_next_async"}

user32 = ctypes.windll.user32
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND] + [ctypes.c_int] * 4 + [ctypes.c_uint]
for _fn in ("IsWindowVisible", "IsIconic", "SetForegroundWindow", "GetWindowTextLengthW"):
    getattr(user32, _fn).argtypes = [wintypes.HWND]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]


# --- logika murni (dicek oleh --selftest) ----------------------------------------
def classify(app_id):
    a = app_id.lower()
    if "spotify" in a:
        return "spotify"
    if any(b in a for b in BROWSERS):
        return "youtube"  # ponytail: semua media dari browser dianggap YouTube; cek judul tab kalau perlu lebih teliti
    return None


def fmt_left(seconds):
    total_min = max(0, int(seconds)) // 60
    d, h, m = total_min // 1440, total_min // 60 % 24, total_min % 60
    return f"{d} h {h} j" if d else f"{h} j {m} m"


def fmt_time(s):
    s = int(s)
    return f"{s // 3600}:{s // 60 % 60:02}:{s % 60:02}" if s >= 3600 else f"{s // 60}:{s % 60:02}"


def parse_claude(status):
    """Ambil limit 5 jam & mingguan dari JSON yang dikirim Claude Code ke status line."""
    out = {}
    for key in ("five_hour", "seven_day"):
        r = (status.get("rate_limits") or {}).get(key) or {}
        if isinstance(r.get("used_percentage"), (int, float)):
            out[key] = (round(r["used_percentage"]), r.get("resets_at") or 0)
    return out or None


JAPANESE = re.compile(r"[\u3040-\u30ff\u4e00-\u9fff]")
_kakasi = None


def romanize(line):
    """Kanji/hiragana/katakana -> romaji. Baris tanpa huruf Jepang dibiarkan."""
    global _kakasi
    if not JAPANESE.search(line):
        return line
    try:
        if _kakasi is None:
            import pykakasi
            _kakasi = pykakasi.kakasi()
    except ImportError:
        return line
    return " ".join(" ".join(part["hepburn"] for part in _kakasi.convert(line)).split())


LRC_LINE = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\](.*)")


def parse_lrc(text):
    lines = []
    for raw in text.splitlines():
        m = LRC_LINE.match(raw.strip())
        if m:
            lines.append((int(m[1]) * 60 + float(m[2]), romanize(m[3].strip())))
    return sorted(lines)


def lyric_at(lines, pos):
    """(baris sekarang, baris berikutnya) pada detik `pos`. Sebelum baris pertama = intro."""
    i = bisect.bisect_right([t for t, _ in lines], pos) - 1
    now = lines[i][1] if i >= 0 else ""
    nxt = lines[i + 1][1] if i + 1 < len(lines) else ""
    return now or "♪", nxt


def fetch_lyrics(artist, title, duration):
    """Lirik bertimestamp dari LRCLIB: coba pencocokan persis, lalu pencarian bebas."""
    exact = urllib.parse.urlencode({"artist_name": artist, "track_name": title, "duration": round(duration)})
    search = urllib.parse.urlencode({"q": f"{artist} {title}"})
    for url in (f"{LRCLIB}/get?{exact}", f"{LRCLIB}/search?{search}"):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "TaskbarIsland/1.0"})
            with urllib.request.urlopen(req, timeout=8) as r:
                found = json.load(r)
        except (OSError, ValueError):  # 404, offline, JSON rusak
            continue
        for item in found if isinstance(found, list) else [found]:
            if item.get("syncedLyrics"):
                return parse_lrc(item["syncedLyrics"])
    return []  # ponytail: gagal = tanpa lirik untuk lagu ini, tidak dicoba ulang


def read_claude():
    try:
        return parse_claude(json.loads(STATUS_FILE.read_text(encoding="utf-8")))
    except (OSError, ValueError):  # file belum ada / sedang ditulis
        return None


# --- Windows ----------------------------------------------------------------------
def animations_enabled():
    """Ikuti setelan Windows 'Animation effects'; kalau dimatikan, kartu langsung berganti tanpa animasi."""
    on = wintypes.BOOL(True)
    ctypes.windll.user32.SystemParametersInfoW(0x1042, 0, ctypes.byref(on), 0)  # SPI_GETCLIENTAREAANIMATION
    return bool(on.value)


def focus_window(*needles):
    """Munculkan jendela pertama yang judulnya mengandung salah satu kata kunci (urut prioritas)."""
    windows = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def collect(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            n = user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            windows.append((hwnd, buf.value.lower()))
        return True

    user32.EnumWindows(collect, 0)
    for needle in filter(None, needles):
        for hwnd, title in windows:
            if needle.lower() in title:
                if user32.IsIconic(hwnd):
                    user32.ShowWindow(hwnd, 9)  # SW_RESTORE, hanya kalau sedang di-minimize
                user32.SetForegroundWindow(hwnd)
                return True
    return False


def open_source(kind, data):
    if kind == "spotify":
        os.startfile("spotify:")
    elif kind == "youtube":
        # Judul jendela browser = judul tab aktif; cocokkan judul video dulu, lalu jendela YouTube mana pun.
        focus_window((data or {}).get("title", "")[:30], "youtube")
    else:
        webbrowser.open("https://claude.ai/settings/usage")


def load_fonts():
    for f in (Path(__file__).parent / "fonts").glob("*.ttf"):
        QFontDatabase.addApplicationFont(str(f))


def load_icon(spec, size=32):
    if spec.startswith("appx:"):  # aplikasi Store: cari folder instalasinya lewat PowerShell
        package, exe = spec[5:].split("|")
        cmd = f"(Get-AppxPackage {package}).InstallLocation"
        folder = subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True, text=True,
                                creationflags=0x08000000).stdout.strip()  # CREATE_NO_WINDOW
        spec = os.path.join(folder, exe)
    if spec.lower().endswith(".ico"):
        return QPixmap(spec).scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    return QFileIconProvider().icon(QFileInfo(spec)).pixmap(size, size)


# --- sumber data ------------------------------------------------------------------
async def read_thumb(ref):
    from winrt.windows.storage.streams import Buffer, InputStreamOptions
    if not ref:
        return b""
    stream = await ref.open_read_async()
    buf = Buffer(stream.size)
    await stream.read_async(buf, buf.capacity, InputStreamOptions.READ_AHEAD)
    return bytes(buf)


def live_position(tl, playing):
    pos, dur = tl.position.total_seconds(), tl.end_time.total_seconds()
    if playing:
        try:
            elapsed = (datetime.now(timezone.utc) - tl.last_updated_time).total_seconds()
            if 0 < elapsed < dur:
                pos += elapsed
        except TypeError:
            pass
    return min(pos, dur), dur


class Media(QObject):
    """Tiap detik: baca Spotify & YouTube dari Windows, lirik dari LRCLIB, Claude usage dari file status line."""
    changed = pyqtSignal(dict)

    def __init__(self):
        super().__init__()
        self.sessions, self.lyrics = {}, {}
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        asyncio.run_coroutine_threadsafe(self.poll(), self.loop)

    async def load_lyrics(self, key, duration):
        self.lyrics[key] = await self.loop.run_in_executor(None, fetch_lyrics, key[1], key[0], duration)

    async def poll(self):
        from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as MediaManager
        manager = await MediaManager.request_async()
        covers = {}
        while True:
            data, sessions = {}, {}
            try:
                for s in manager.get_sessions():
                    kind = classify(s.source_app_user_model_id)
                    playing = s.get_playback_info().playback_status == PLAYING
                    if not kind or (kind in data and (data[kind]["playing"] or not playing)):
                        continue  # satu kartu per jenis; yang sedang diputar menang
                    props = await s.try_get_media_properties_async()
                    key = (props.title, props.artist)
                    if covers.get(kind, (None,))[0] != key:
                        covers[kind] = (key, await read_thumb(props.thumbnail))
                    pos, dur = live_position(s.get_timeline_properties(), playing)
                    if kind == "spotify" and key not in self.lyrics and dur > 0:
                        self.lyrics[key] = None  # sedang dicari
                        self.loop.create_task(self.load_lyrics(key, dur))
                    data[kind] = {"title": props.title, "artist": props.artist, "cover": covers[kind][1],
                                  "playing": playing, "pos": pos, "dur": dur, "at": time.monotonic(),
                                  "lyrics": self.lyrics.get(key) if kind == "spotify" else None}
                    sessions[kind] = s
            except Exception as e:  # sesi bisa hilang di tengah jalan (aplikasi ditutup)
                print("media:", e)
            self.sessions = sessions
            data["claude"] = read_claude()
            self.changed.emit(data)
            await asyncio.sleep(1)

    def command(self, kind, name):
        async def run():
            s = self.sessions.get(kind)
            if s:
                await getattr(s, COMMANDS[name])()
        asyncio.run_coroutine_threadsafe(run(), self.loop)


# --- tampilan ---------------------------------------------------------------------
class Island(QWidget):
    def __init__(self, media):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.media, self.data, self.kind, self.pix, self.t, self.buttons = media, {}, "claude", {}, 0.0, {}
        self.pill_rect = QRectF()
        self.app_icons = {k: load_icon(p) for k, p in APP_ICONS.items()}
        self.launch_icons = {label: load_icon(spec) for label, _, spec in LAUNCHERS}
        media.changed.connect(self.on_media)

        self.anim = QVariantAnimation(self, duration=200, easingCurve=QEasingCurve.OutCubic)
        self.anim.valueChanged.connect(self.set_t)
        self.anim.finished.connect(lambda: self.t == 0 and self.place(False))
        self.collapse = QTimer(self, singleShot=True, interval=350, timeout=lambda: self.animate(0))

        # animasi pindah kartu: kartu lama terlempar ke samping, kartu baru naik dari tumpukan
        self.prev_kind, self.dir, self.swap_s = None, 1, 1.0
        self.swap = QVariantAnimation(self, startValue=0.0, endValue=1.0, duration=280, easingCurve=QEasingCurve.OutCubic)
        self.swap.valueChanged.connect(lambda v: (setattr(self, "swap_s", v), self.update()))
        if not animations_enabled():
            self.anim.setDuration(0)
            self.swap.setDuration(0)

        QTimer(self, interval=500, timeout=self.keep_on_top).start()
        self.mq_text, self.mq_t0, self.mq_active = "", 0.0, False
        QTimer(self, interval=40, timeout=self.tick).start()
        self.place(False)

    # posisi & animasi
    def place(self, expanded):
        g, a = self.screen().geometry(), self.screen().availableGeometry()
        self.bar_h = (g.bottom() - a.bottom()) or 48   # ponytail: anggap taskbar di bawah; auto-hide pakai 48
        h = self.bar_h + (GAP + PEEK + H_TALL if expanded else 0)
        self.setGeometry(g.left() + X_OFFSET, g.bottom() + 1 - h, CARD_W if expanded else PILL_W, h)

    def set_t(self, t):
        self.t = t
        self.update()

    def animate(self, target):
        if target:
            self.place(True)
        self.anim.stop()
        self.anim.setStartValue(self.t)
        self.anim.setEndValue(float(target))
        self.anim.start()

    def enterEvent(self, _):
        self.collapse.stop()
        if self.t < 1:
            self.animate(1)

    def leaveEvent(self, _):
        self.collapse.start()

    def keep_on_top(self):
        # Sembunyi saat game/video fullscreen, selain itu paksa tetap di atas taskbar.
        state = ctypes.c_int()
        ctypes.windll.shell32.SHQueryUserNotificationState(ctypes.byref(state))
        fullscreen = state.value in (2, 3, 4)
        self.setVisible(not fullscreen)
        if not fullscreen:
            user32.SetWindowPos(int(self.winId()), -1, 0, 0, 0, 0, 0x13)  # TOPMOST, NOMOVE|NOSIZE|NOACTIVATE

    def tick(self):
        # repaint ~25 fps hanya saat ada yang bergerak: equalizer, teks berjalan, atau lirik
        if self.media_playing() or self.mq_active:
            self.mq_active = False
            self.update()

    # data & interaksi
    def cards(self):
        return [k for k in ("spotify", "youtube") if self.data.get(k)] + ["claude"]

    def card_h(self, kind):
        if kind == "claude" or (kind == "spotify" and (self.data.get("spotify") or {}).get("lyrics")):
            return H_TALL
        return H_SHORT

    def pos_now(self, d):
        pos = d["pos"] + (time.monotonic() - d["at"] if d["playing"] else 0)
        return min(pos, d["dur"]) if d["dur"] else pos

    def on_media(self, data):
        playing_before = {k: (self.data.get(k) or {}).get("playing") for k in ("spotify", "youtube")}
        for kind in ("spotify", "youtube"):
            new, old = data.get(kind), self.data.get(kind)
            if new and new["cover"] != (old or {}).get("cover"):
                self.pix[kind] = QPixmap()
                self.pix[kind].loadFromData(new["cover"])
        self.data = data
        for kind in ("spotify", "youtube"):
            new = data.get(kind)
            if new and new["playing"] and not playing_before.get(kind) and kind != self.kind:
                self.switch_to(kind)  # media yang baru mulai diputar langsung tampil
        if self.kind not in self.cards():
            self.kind = self.cards()[0]
        self.update()

    def switch_to(self, kind, step=1):
        self.prev_kind, self.dir, self.kind = self.kind, step, kind
        self.swap.stop()
        self.swap.start()

    def step(self, step):
        cards = self.cards()
        self.switch_to(cards[(cards.index(self.kind) + step) % len(cards)], step)

    def wheelEvent(self, e):
        if self.t > 0.9 and self.swap.state() != QVariantAnimation.Running:
            self.step(-1 if e.angleDelta().y() > 0 else 1)

    def mousePressEvent(self, e):
        if e.button() == Qt.RightButton:
            menu = QMenu()
            menu.addAction("Keluar", QApplication.quit)
            menu.exec_(e.globalPos())
            return
        if self.swap.state() == QVariantAnimation.Running:
            return
        if self.t > 0.9:
            for key, rect in self.buttons.items():
                if rect.contains(e.pos()):
                    if key in ("left", "right"):
                        self.step(1 if key == "right" else -1)
                    elif key.startswith("launch:"):
                        app_id = next(a for label, a, _ in LAUNCHERS if label == key[7:])
                        os.startfile("shell:AppsFolder\\" + app_id)
                    else:
                        self.media.command(self.kind, key)
                    return
        h = self.card_h(self.kind)
        card = QRectF(0, PEEK + H_TALL - h, CARD_W, h)
        if self.pill_rect.contains(e.pos()) or (self.t > 0.9 and card.contains(e.pos())):
            open_source(self.kind, self.data.get(self.kind))  # klik badan kartu = buka sumbernya

    # gambar
    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform | QPainter.TextAntialiasing)
        self.buttons = {}
        if self.t > 0:
            p.fillRect(self.rect(), QColor(0, 0, 0, 1))  # alpha 1: celah transparan tetap terhitung hover
            p.save()
            p.setOpacity(self.t)
            p.translate(0, (1 - self.t) * 10)
            self.paint_card(p)
            p.restore()
        self.paint_pill(p)

    def paint_pill(self, p):
        r = self.pill_rect = QRectF(0.5, self.height() - self.bar_h + (self.bar_h - PILL_H) / 2 + 0.5,
                                    PILL_W - 1, PILL_H - 1)
        self.box(p, r, C["bg"], C["border"], 10)
        self.paint_app_icon(p, self.kind, QRectF(r.left() + 11, r.center().y() - 9, 18, 18))
        if self.media_playing():
            pips_w = 4 * 3 + 3 * 2.5
            self.equalizer(p, r.right() - 12 - pips_w, r.center().y())
        else:  # penanda posisi kartu
            cards = self.cards()
            pips_w = sum(12 if k == self.kind else 4 for k in cards) + 3 * (len(cards) - 1)
            x = r.right() - 12 - pips_w
            for k in cards:
                w = 12 if k == self.kind else 4
                self.box(p, QRectF(x, r.center().y() - 2, w, 4), C["accent"] if k == self.kind else C["pip_off"], None, 2)
                x += w + 3
        tx = r.left() + 11 + 18 + 10
        self.marquee(p, QRectF(tx, r.top(), r.right() - 12 - pips_w - 12 - tx, r.height()), self.compact_text(), 12,
                     C["text"])

    def media_playing(self):
        return self.kind != "claude" and (self.data.get(self.kind) or {}).get("playing")

    def equalizer(self, p, x, cy):
        # ponytail: gerakan bar animasi tetap, bukan dari suara asli; pakai peak meter WASAPI kalau mau reaktif
        t = time.monotonic()
        for i, phase in enumerate((0.0, 1.7, 0.9, 2.6)):
            h = 4 + 10 * (0.5 + 0.5 * math.sin(t * 7 + phase)) * (0.6 + 0.4 * math.sin(t * 2.3 + phase * 2))
            self.box(p, QRectF(x + i * 5.5, cy + 7 - h, 3, h), C["accent"], None, 1.5)

    def marquee(self, p, rect, s, px, color):
        """Teks yang kepanjangan berjalan ke kiri: diam 1,2 dtk, jalan 30 px/dtk, diam 1,2 dtk, ulang."""
        f = self.make_font(px)
        tw = QFontMetrics(f).horizontalAdvance(s)
        if tw <= rect.width():
            self.text(p, rect, s, px, color)
            return
        if s != self.mq_text:
            self.mq_text, self.mq_t0 = s, time.monotonic()
        self.mq_active = True
        overflow = tw - rect.width()
        phase = (time.monotonic() - self.mq_t0) % (overflow / 30 + 2.4)
        off = 0 if phase < 1.2 else min(overflow, (phase - 1.2) * 30)
        p.save()
        p.setClipRect(rect)
        p.setFont(f)
        p.setPen(QColor(color))
        p.drawText(QRectF(rect.left() - off, rect.top(), tw + 4, rect.height()), Qt.AlignLeft | Qt.AlignVCenter, s)
        p.restore()

    def compact_text(self):
        d = self.data.get(self.kind)
        if self.kind == "claude":
            if not d or "five_hour" not in d:
                return "Belum ada data"
            pct, reset = d["five_hour"]
            left = reset - datetime.now(timezone.utc).timestamp()
            return f"Sesi {pct}% · reset {fmt_left(left)}" if reset else f"Sesi {pct}%"
        if self.kind == "spotify" and d["lyrics"] and d["playing"]:
            return lyric_at(d["lyrics"], self.pos_now(d))[0]  # baris lirik yang sedang dinyanyikan
        return f"{d['title']} · {d['artist']}" if self.kind == "spotify" and d["artist"] else d["title"]

    def paint_card(self, p):
        s = self.swap_s
        swapping = s < 1 and self.prev_kind in self.cards()
        h = self.card_h(self.kind)
        if swapping:  # tumpukan ikut menyesuaikan tinggi kartu
            h = self.card_h(self.prev_kind) + (h - self.card_h(self.prev_kind)) * s
        top = PEEK + H_TALL - h
        self.box(p, QRectF(24.5, top - PEEK + 0.5, CARD_W - 49, h - 7), C["back2"], C["back2_border"], 16)
        self.box(p, QRectF(12.5, top - PEEK + 7.5, CARD_W - 25, h - 4), C["back1"], C["back1_border"], 16)
        if swapping:
            self.paint_front(p, self.kind, dy=-PEEK * (1 - s), scale=0.94 + 0.06 * s)  # kartu baru naik dari tumpukan
            self.paint_front(p, self.prev_kind, dx=self.dir * 50 * s, opacity=max(0.0, 1 - 2.5 * s))  # kartu lama terlempar
        else:
            self.paint_front(p, self.kind)

    def paint_front(self, p, kind, dx=0.0, dy=0.0, scale=1.0, opacity=1.0):
        h = self.card_h(kind)
        ct = PEEK + H_TALL - h  # card top
        p.save()
        p.setOpacity(p.opacity() * opacity)
        p.translate(CARD_W / 2 + dx, ct + h / 2 + dy)
        p.scale(scale, scale)
        p.translate(-CARD_W / 2, -(ct + h / 2))
        self.box(p, QRectF(0.5, ct + 0.5, CARD_W - 1, h - 1), C["bg"], C["border"], 16)

        x0, y0 = 18, ct + 8
        cards = self.cards()
        right = QRectF(CARD_W - 8 - 36, y0, 36, 36)
        left = QRectF(right.left() - 36, y0, 36, 36)
        self.paint_app_icon(p, kind, QRectF(x0, y0 + 8, 20, 20))
        self.text(p, QRectF(left.left() - 60, y0, 56, 36), f"{cards.index(kind) + 1} / {len(cards)}", 11,
                  C["muted"], align=Qt.AlignRight | Qt.AlignVCenter)
        self.icon(p, "left", left, GLYPH["left"], 12, C["text"])
        self.icon(p, "right", right, GLYPH["right"], 12, C["text"])

        y = y0 + 36 + 10
        if kind == "claude":
            self.paint_claude(p, x0, y)
        else:
            self.paint_media(p, kind, x0, y, self.data[kind])
        p.restore()

    def paint_media(self, p, kind, x0, y, d):
        art = QRectF(x0, y, 52 if kind == "spotify" else 92, 52)
        self.draw_cover(p, art, self.pix.get(kind))
        tx = art.right() + 12
        self.text(p, QRectF(tx, y + 6, CARD_W - 18 - tx, 20), d["title"], 14, C["text"], QFont.DemiBold)
        self.text(p, QRectF(tx, y + 28, CARD_W - 18 - tx, 18), d["artist"], 12, C["muted"])

        row = y + 52 + 14
        nxt = QRectF(CARD_W - 18 - 36, row + 2, 36, 36)
        play = QRectF(nxt.left() - 12 - 40, row, 40, 40)
        prv = QRectF(play.left() - 12 - 36, row + 2, 36, 36)
        self.icon(p, "prev", prv, GLYPH["prev"], 14, C["text"])
        self.box(p, play, C["accent"], None, 20)
        self.icon(p, "play", play, GLYPH["pause" if d["playing"] else "play"], 14, C["on_accent"])
        self.icon(p, "next", nxt, GLYPH["next"], 14, C["text"])

        pos = self.pos_now(d)
        if d["dur"] > 0:
            bar = QRectF(x0, row + 12, prv.left() - 12 - x0, 3)
            self.box(p, bar, C["track"], None, 1.5)
            self.box(p, QRectF(bar.left(), bar.top(), bar.width() * pos / d["dur"], 3), C["accent"], None, 1.5)
            t = QRectF(bar.left(), bar.bottom() + 5, bar.width(), 14)
            self.text(p, t, fmt_time(pos), 11, C["muted"])
            self.text(p, t, fmt_time(d["dur"]), 11, C["muted"], align=Qt.AlignRight | Qt.AlignVCenter)

        if kind == "spotify" and d["lyrics"]:
            now, nxt_line = lyric_at(d["lyrics"], pos)
            w = CARD_W - 2 * x0
            ly = row + 48
            if QFontMetrics(self.make_font(13, QFont.DemiBold)).horizontalAdvance(now) <= w:
                self.text(p, QRectF(x0, ly, w, 20), now, 13, C["accent"], QFont.DemiBold)
                self.text(p, QRectF(x0, ly + 20, w, 18), nxt_line, 12, C["muted"])
            else:  # baris panjang: pecah jadi 2 baris, baris berikutnya disembunyikan dulu
                self.text(p, QRectF(x0, ly, w, 46), now, 13, C["accent"], QFont.DemiBold,
                          align=Qt.AlignLeft | Qt.AlignTop | Qt.TextWordWrap)

    def paint_claude(self, p, x0, y):
        d = self.data.get("claude")
        w = CARD_W - 2 * x0
        if not d:
            self.text(p, QRectF(x0, y, w, 20), "Belum ada data.", 13, C["text"])
            self.text(p, QRectF(x0, y + 22, w, 18), "Kirim satu pesan di Claude Code dulu.", 12, C["muted"])
        else:
            now = datetime.now(timezone.utc).timestamp()
            for i, (name, key) in enumerate((("Sesi 5 jam", "five_hour"), ("Mingguan", "seven_day"))):
                if key not in d:
                    continue
                ry = y + i * 46
                pct, reset = d[key]
                self.text(p, QRectF(x0, ry, w, 18), name, 12, C["text"])
                self.text(p, QRectF(x0, ry, w, 18), f"{pct}%", 15, C["text"], QFont.DemiBold,
                          align=Qt.AlignRight | Qt.AlignVCenter)
                self.box(p, QRectF(x0, ry + 22, w, 4), C["track"], None, 2)
                self.box(p, QRectF(x0, ry + 22, w * min(pct, 100) / 100, 4), C["accent"], None, 2)
                if reset:
                    self.text(p, QRectF(x0, ry + 29, w, 14), f"Reset dalam {fmt_left(reset - now)}", 11, C["muted"])

        # launcher: tile ikon + nama
        gap = 8
        tw = (w - gap * (len(LAUNCHERS) - 1)) / len(LAUNCHERS)
        ty = y + 46 * 2 + 8
        for i, (label, _, _) in enumerate(LAUNCHERS):
            tile = QRectF(x0 + i * (tw + gap), ty, tw, 46)
            self.box(p, tile, C["tile"], C["border"], 10)
            p.drawPixmap(QRectF(tile.center().x() - 9, tile.top() + 7, 18, 18), self.launch_icons[label],
                         QRectF(self.launch_icons[label].rect()))
            self.text(p, QRectF(tile.left() + 2, tile.top() + 28, tile.width() - 4, 14), label, 10, C["muted"],
                      align=Qt.AlignCenter)
            self.buttons["launch:" + label] = tile

    # helper gambar
    def paint_app_icon(self, p, kind, rect):
        pm = self.app_icons.get(kind)
        if pm is not None and not pm.isNull():
            p.drawPixmap(rect, pm, QRectF(pm.rect()))
            return
        # Claude: bintang sederhana berwarna oranye
        p.save()
        p.setPen(QPen(QColor(C["claude"]), 2, Qt.SolidLine, Qt.RoundCap))
        c, r = rect.center(), rect.width() / 2
        for i in range(4):
            v = QPointF(*[(r, 0), (0, r), (r * 0.7, r * 0.7), (r * 0.7, -r * 0.7)][i])
            p.drawLine(c - v, c + v)
        p.restore()

    def box(self, p, rect, fill, border, radius):
        p.setPen(QColor(border) if border else Qt.NoPen)
        p.setBrush(QColor(fill))
        p.drawRoundedRect(rect, radius, radius)

    def draw_cover(self, p, rect, pm):
        path = QPainterPath()
        path.addRoundedRect(rect, 10, 10)
        p.save()
        p.setClipPath(path)
        if not pm or pm.isNull():
            p.fillRect(rect, QColor(C["art"]))
        else:  # potong tengah mengikuti rasio kotak
            ratio = rect.width() / rect.height()
            sw = min(pm.width(), pm.height() * ratio)
            sh = sw / ratio
            p.drawPixmap(rect, pm, QRectF((pm.width() - sw) / 2, (pm.height() - sh) / 2, sw, sh))
        p.restore()

    def icon(self, p, key, rect, glyph, px, color):
        self.buttons[key] = rect
        self.text(p, rect, glyph, px, color, align=Qt.AlignCenter, family=ICON_FONT)

    @staticmethod
    def make_font(px, weight=QFont.Normal, family=FONT):
        f = QFont(family)
        f.setPixelSize(px)
        f.setWeight(weight)
        return f

    def text(self, p, rect, s, px, color, weight=QFont.Normal, align=Qt.AlignLeft | Qt.AlignVCenter, family=FONT):
        f = self.make_font(px, weight, family)
        p.setFont(f)
        p.setPen(QColor(color))
        if not align & Qt.TextWordWrap:
            s = QFontMetrics(f).elidedText(s or "", Qt.ElideRight, max(0, int(rect.width())))
        p.drawText(rect, align, s or "")


def selftest():
    assert classify("Spotify.exe") == "spotify"
    assert classify("Brave") == "youtube"
    assert classify("Microsoft.ZuneMusic") is None
    assert fmt_left(2 * 3600 + 14 * 60 + 30) == "2 j 14 m"
    assert fmt_left(3 * 86400 + 4 * 3600) == "3 h 4 j"
    assert fmt_time(84) == "1:24" and fmt_time(3725) == "1:02:05"
    st = {"rate_limits": {"five_hour": {"used_percentage": 61.6, "resets_at": 1700000000}}}
    assert parse_claude(st) == {"five_hour": (62, 1700000000)}
    assert parse_claude({}) is None
    lrc = parse_lrc("[00:31.06] When we were made\n[00:39.10] It was no accident\n[01:00.00]\nbukan baris")
    assert lrc == [(31.06, "When we were made"), (39.1, "It was no accident"), (60.0, "")]
    assert lyric_at(lrc, 10) == ("♪", "When we were made")
    assert lyric_at(lrc, 35) == ("When we were made", "It was no accident")
    assert lyric_at(lrc, 61) == ("♪", "")
    assert romanize("hello") == "hello"
    assert romanize("夜に駆ける").startswith("yoru ni")
    print("selftest OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
        sys.exit()
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling)
    app = QApplication(sys.argv)
    load_fonts()
    island = Island(Media())
    island.show()
    sys.exit(app.exec_())
