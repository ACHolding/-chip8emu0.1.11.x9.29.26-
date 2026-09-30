# program.py
# cat's chip8emu 0.1.1 - Chip-8 emulator, single file, tkinter
# Window: 600x400, centered on screen, black bg, blue-hue text.
# FILES = OFF  (no config / save files written - settings live in memory)
# Menu strip (top of window): Load ROM | Help | About | Exit | Controls

import tkinter as tk
from tkinter import filedialog
import random
import sys
import time
import array
import shutil
import threading
import subprocess

FILES = False   # files = off: nothing is ever written to disk

# ---------------------------------------------------------------------------
# Chip-8 font set (5 bytes per hex digit, 0-F), loaded at 0x050
# ---------------------------------------------------------------------------
FONT_SET = [
    0xF0, 0x90, 0x90, 0x90, 0xF0,  # 0
    0x20, 0x60, 0x20, 0x20, 0x70,  # 1
    0xF0, 0x10, 0xF0, 0x80, 0xF0,  # 2
    0xF0, 0x10, 0xF0, 0x10, 0xF0,  # 3
    0x90, 0x90, 0xF0, 0x10, 0x10,  # 4
    0xF0, 0x80, 0xF0, 0x10, 0xF0,  # 5
    0xF0, 0x80, 0xF0, 0x90, 0xF0,  # 6
    0xF0, 0x10, 0x20, 0x40, 0x40,  # 7
    0xF0, 0x90, 0xF0, 0x90, 0xF0,  # 8
    0xF0, 0x90, 0xF0, 0x10, 0xF0,  # 9
    0xF0, 0x90, 0xF0, 0x90, 0x90,  # A
    0xE0, 0x90, 0xE0, 0x90, 0xE0,  # B
    0xF0, 0x80, 0x80, 0x80, 0xF0,  # C
    0xE0, 0x90, 0x90, 0x90, 0xE0,  # D
    0xF0, 0x80, 0xF0, 0x80, 0xF0,  # E
    0xF0, 0x80, 0xF0, 0x80, 0x80,  # F
]

# Chip-8 keypad -> PC keyboard
KEYMAP = {
    '1': 0x1, '2': 0x2, '3': 0x3, '4': 0xC,
    'q': 0x4, 'w': 0x5, 'e': 0x6, 'r': 0xD,
    'a': 0x7, 's': 0x8, 'd': 0x9, 'f': 0xE,
    'z': 0xA, 'x': 0x0, 'c': 0xB, 'v': 0xF,
}

# ---------------------------------------------------------------------------
# colors
# ---------------------------------------------------------------------------
BG      = "black"
HUE     = "#4488ff"   # main blue
HUE_DIM = "#2266cc"   # dimmer blue
HUE_HI  = "#88bbff"   # bright blue
EDGE    = "#0044ff"   # border blue
BG_HOV  = "#001133"   # hover / active bg

PIXEL_COLORS = {
    "Blue":  "#4488ff",
    "Green": "#33ff66",
    "Amber": "#ffb000",
    "White": "#ffffff",
}

SPEEDS = {
    "Slow (5)":     5,
    "Normal (10)": 10,
    "Fast (20)":   20,
    "Turbo (40)":  40,
}


AUDIO_TONES = {
    "Low (220 Hz)":  220,
    "Mid (440 Hz)":  440,
    "High (880 Hz)": 880,
}

AUDIO_VOLUMES = {
    "Low":    0.10,
    "Medium": 0.25,
    "High":   0.50,
}


# ---------------------------------------------------------------------------
# chip8audio 0.1  -  buzzer audio engine
# ---------------------------------------------------------------------------
# The Chip-8 has one sound: a buzzer that plays while sound_timer > 0.
# chip8audio synthesizes a square wave entirely in memory (FILES = OFF,
# no .wav is ever written) and plays it through the first backend that
# works on this machine:
#
#   1. pygame.mixer        (if pygame is installed)       - any OS
#   2. winsound.Beep       (built into python)            - Windows
#   3. aplay / paplay pipe (raw PCM streamed over stdin)  - Linux
#   4. tk bell             (always works, one blip/beep)  - fallback
#
# Usage from the emulator loop (main thread, once per frame):
#       audio.set_active(cpu.sound_timer > 0)
# ---------------------------------------------------------------------------
class Chip8Audio:
    NAME = "chip8audio"
    VERSION = "0.1"
    RATE = 22050

    def __init__(self, root, freq=440, volume=0.25):
        self.root = root
        self.freq = freq
        self.volume = volume
        self.enabled = True
        self.active = False
        self.backend = "none"

        self._lock = threading.Lock()
        self._on = threading.Event()        # tone should be sounding
        self._quit = threading.Event()
        self._thread = None
        self._proc = None
        self._phase = 0
        self._pg_sound = None
        self._pg = None
        self._winsound = None

        for init in (self._init_pygame, self._init_winsound,
                     self._init_pipe, self._init_bell):
            try:
                if init():
                    break
            except Exception:
                continue

    # ------------------------------------------------------------------
    # wave synthesis (in memory)
    # ------------------------------------------------------------------
    def _square(self, n_samples, channels=1):
        """n_samples of phase-continuous signed 16-bit square wave."""
        amp = int(32767 * max(0.0, min(1.0, self.volume)))
        period = max(2, self.RATE // max(1, self.freq))
        half = period // 2
        buf = array.array("h")
        ph = self._phase
        for _ in range(n_samples):
            s = amp if (ph % period) < half else -amp
            for _c in range(channels):
                buf.append(s)
            ph += 1
        self._phase = ph % period
        if sys.byteorder != "little":
            buf.byteswap()
        return buf.tobytes()

    # ------------------------------------------------------------------
    # backend 1: pygame.mixer
    # ------------------------------------------------------------------
    def _init_pygame(self):
        import pygame                           # optional
        if not pygame.mixer.get_init():
            pygame.mixer.pre_init(self.RATE, -16, 1, 512)
            pygame.mixer.init()
        freq, size, ch = pygame.mixer.get_init()
        if size != -16:
            pygame.mixer.quit()
            return False
        self.RATE = freq
        self._pg = pygame
        self._pg_channels = ch
        self._build_pg_sound()
        self.backend = "pygame.mixer"
        return True

    def _build_pg_sound(self):
        # one full second loops seamlessly because RATE % period == 0-ish;
        # use an exact whole number of periods to avoid clicks
        period = max(2, self.RATE // max(1, self.freq))
        n = period * max(1, (self.RATE // 4) // period)
        self._phase = 0
        raw = self._square(n, self._pg_channels)
        self._pg_sound = self._pg.mixer.Sound(buffer=raw)

    # ------------------------------------------------------------------
    # backend 2: winsound (Windows)
    # ------------------------------------------------------------------
    def _init_winsound(self):
        if not sys.platform.startswith("win"):
            return False
        import winsound
        self._winsound = winsound
        self.backend = "winsound"
        self._start_thread(self._winsound_loop)
        return True

    def _winsound_loop(self):
        while not self._quit.is_set():
            if self._on.wait(0.05) and not self._quit.is_set():
                try:
                    f = max(37, min(32767, self.freq))
                    self._winsound.Beep(f, 40)      # short slices
                except Exception:
                    time.sleep(0.04)

    # ------------------------------------------------------------------
    # backend 3: aplay / paplay pipe (Linux)
    # ------------------------------------------------------------------
    def _init_pipe(self):
        if sys.platform.startswith("win") or sys.platform == "darwin":
            return False
        cmd = None
        if shutil.which("paplay"):
            cmd = ["paplay", "--raw", "--format=s16le",
                   "--rate=%d" % self.RATE, "--channels=1",
                   "--latency-msec=40"]
        elif shutil.which("aplay"):
            cmd = ["aplay", "-q", "-t", "raw", "-f", "S16_LE",
                   "-r", str(self.RATE), "-c", "1",
                   "--buffer-time=60000"]
        if not cmd:
            return False
        self._proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(0.05)
        if self._proc.poll() is not None:
            self._proc = None
            return False
        self.backend = cmd[0] + " pipe"
        self._start_thread(self._pipe_loop)
        return True

    def _pipe_loop(self):
        chunk = self.RATE // 100                 # 10 ms per write
        lead = 0.04                              # stay 40 ms ahead
        while not self._quit.is_set():
            if not self._on.wait(0.05):
                continue
            t0 = time.monotonic()
            written = 0
            while self._on.is_set() and not self._quit.is_set():
                ahead = written / self.RATE - (time.monotonic() - t0)
                if ahead < lead:
                    with self._lock:
                        data = self._square(chunk)
                    try:
                        self._proc.stdin.write(data)
                        self._proc.stdin.flush()
                    except (BrokenPipeError, OSError, ValueError):
                        self._proc = None
                        self.backend = "tk bell"
                        return
                    written += chunk
                else:
                    time.sleep(0.005)

    # ------------------------------------------------------------------
    # backend 4: tk bell
    # ------------------------------------------------------------------
    def _init_bell(self):
        self.backend = "tk bell"
        return True

    # ------------------------------------------------------------------
    def _start_thread(self, target):
        self._thread = threading.Thread(target=target, daemon=True)
        self._thread.start()

    # ------------------------------------------------------------------
    # public api
    # ------------------------------------------------------------------
    def set_active(self, on):
        """Call once per frame with (sound_timer > 0)."""
        on = bool(on) and self.enabled
        if on == self.active:
            return
        self.active = on

        if self.backend == "pygame.mixer" and self._pg_sound is not None:
            if on:
                self._pg_sound.play(loops=-1)
            else:
                self._pg_sound.stop()
        elif self.backend == "tk bell":
            if on:
                try:
                    self.root.bell()
                except tk.TclError:
                    pass
        else:
            if on:
                self._on.set()
            else:
                self._on.clear()

    def silence(self):
        self.set_active(False)

    def set_enabled(self, enabled):
        self.enabled = bool(enabled)
        if not self.enabled:
            self.silence()

    def set_freq(self, freq):
        was = self.active
        self.silence()
        with self._lock:
            self.freq = int(freq)
            if self.backend == "pygame.mixer":
                self._build_pg_sound()
        if was:
            self.set_active(True)

    def set_volume(self, volume):
        was = self.active
        self.silence()
        with self._lock:
            self.volume = float(volume)
            if self.backend == "pygame.mixer":
                self._build_pg_sound()
        if was:
            self.set_active(True)

    def close(self):
        self.silence()
        self._quit.set()
        self._on.set()                  # wake worker so it can exit
        if self._proc is not None:
            try:
                self._proc.stdin.close()
            except Exception:
                pass
            try:
                self._proc.terminate()
            except Exception:
                pass
            self._proc = None
        if self._pg is not None:
            try:
                self._pg.mixer.quit()
            except Exception:
                pass

    def describe(self):
        return "%s %s  (%s)" % (self.NAME, self.VERSION, self.backend)


# ---------------------------------------------------------------------------
# Chip-8 CPU
# ---------------------------------------------------------------------------
class Chip8:
    def __init__(self):
        self.reset()

    def load_font(self):
        for i, b in enumerate(FONT_SET):
            self.memory[0x50 + i] = b

    def reset(self):
        self.memory = [0] * 4096
        self.V = [0] * 16
        self.I = 0
        self.pc = 0x200
        self.stack = []
        self.delay_timer = 0
        self.sound_timer = 0
        self.display = [[0] * 64 for _ in range(32)]
        self.keys = [0] * 16
        self.waiting_key = None
        self.draw_flag = True
        self.load_font()

    # ------------------------------------------------------------------
    def cycle(self):
        if self.waiting_key is not None:
            for i in range(16):
                if self.keys[i]:
                    self.V[self.waiting_key] = i
                    self.waiting_key = None
                    break
            return

        op = (self.memory[self.pc] << 8) | self.memory[(self.pc + 1) & 0xFFF]
        self.pc = (self.pc + 2) & 0xFFF
        self.execute(op)

    # ------------------------------------------------------------------
    def execute(self, op):
        n1 = (op & 0xF000) >> 12
        x  = (op & 0x0F00) >> 8
        y  = (op & 0x00F0) >> 4
        n  = op & 0x000F
        nn = op & 0x00FF
        nnn = op & 0x0FFF

        if op == 0x00E0:
            self.display = [[0] * 64 for _ in range(32)]
            self.draw_flag = True
        elif op == 0x00EE:
            if self.stack:
                self.pc = self.stack.pop()
        elif n1 == 0x0:
            pass
        elif n1 == 0x1:
            self.pc = nnn
        elif n1 == 0x2:
            self.stack.append(self.pc)
            self.pc = nnn
        elif n1 == 0x3:
            if self.V[x] == nn:
                self.pc = (self.pc + 2) & 0xFFF
        elif n1 == 0x4:
            if self.V[x] != nn:
                self.pc = (self.pc + 2) & 0xFFF
        elif n1 == 0x5 and n == 0:
            if self.V[x] == self.V[y]:
                self.pc = (self.pc + 2) & 0xFFF
        elif n1 == 0x6:
            self.V[x] = nn
        elif n1 == 0x7:
            self.V[x] = (self.V[x] + nn) & 0xFF
        elif n1 == 0x8:
            self._op8(x, y, n)
        elif n1 == 0x9 and n == 0:
            if self.V[x] != self.V[y]:
                self.pc = (self.pc + 2) & 0xFFF
        elif n1 == 0xA:
            self.I = nnn
        elif n1 == 0xB:
            self.pc = (nnn + self.V[0]) & 0xFFF
        elif n1 == 0xC:
            self.V[x] = random.randint(0, 255) & nn
        elif n1 == 0xD:
            self._op_draw(x, y, n)
        elif n1 == 0xE:
            if nn == 0x9E:
                if self.keys[self.V[x] & 0xF]:
                    self.pc = (self.pc + 2) & 0xFFF
            elif nn == 0xA1:
                if not self.keys[self.V[x] & 0xF]:
                    self.pc = (self.pc + 2) & 0xFFF
        elif n1 == 0xF:
            self._op_f(x, nn)

    # ------------------------------------------------------------------
    def _op8(self, x, y, n):
        if n == 0x0:
            self.V[x] = self.V[y]
        elif n == 0x1:
            self.V[x] |= self.V[y]
        elif n == 0x2:
            self.V[x] &= self.V[y]
        elif n == 0x3:
            self.V[x] ^= self.V[y]
        elif n == 0x4:
            s = self.V[x] + self.V[y]
            self.V[x] = s & 0xFF
            self.V[0xF] = 1 if s > 0xFF else 0
        elif n == 0x5:
            f = 1 if self.V[x] >= self.V[y] else 0
            self.V[x] = (self.V[x] - self.V[y]) & 0xFF
            self.V[0xF] = f
        elif n == 0x6:
            f = self.V[x] & 0x1
            self.V[x] = (self.V[x] >> 1) & 0xFF
            self.V[0xF] = f
        elif n == 0x7:
            f = 1 if self.V[y] >= self.V[x] else 0
            self.V[x] = (self.V[y] - self.V[x]) & 0xFF
            self.V[0xF] = f
        elif n == 0xE:
            f = (self.V[x] >> 7) & 1
            self.V[x] = (self.V[x] << 1) & 0xFF
            self.V[0xF] = f

    # ------------------------------------------------------------------
    def _op_draw(self, x, y, n):
        vx = self.V[x] % 64
        vy = self.V[y] % 32
        self.V[0xF] = 0
        for row in range(n):
            pixel = self.memory[(self.I + row) & 0xFFF]
            for col in range(8):
                if pixel & (0x80 >> col):
                    px = (vx + col) % 64
                    py = (vy + row) % 32
                    if self.display[py][px]:
                        self.V[0xF] = 1
                    self.display[py][px] ^= 1
        self.draw_flag = True

    # ------------------------------------------------------------------
    def _op_f(self, x, nn):
        if nn == 0x07:
            self.V[x] = self.delay_timer
        elif nn == 0x0A:
            self.waiting_key = x
        elif nn == 0x15:
            self.delay_timer = self.V[x]
        elif nn == 0x18:
            self.sound_timer = self.V[x]
        elif nn == 0x1E:
            self.I = (self.I + self.V[x]) & 0xFFF
        elif nn == 0x29:
            self.I = 0x50 + (self.V[x] & 0xF) * 5
        elif nn == 0x33:
            self.memory[self.I & 0xFFF]       = self.V[x] // 100
            self.memory[(self.I + 1) & 0xFFF] = (self.V[x] // 10) % 10
            self.memory[(self.I + 2) & 0xFFF] = self.V[x] % 10
        elif nn == 0x55:
            for i in range(x + 1):
                self.memory[(self.I + i) & 0xFFF] = self.V[i]
        elif nn == 0x65:
            for i in range(x + 1):
                self.V[i] = self.memory[(self.I + i) & 0xFFF]


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------
class CatChip8Emu:
    WIN_W, WIN_H = 600, 400
    SCALE = 9                       # 64*9 = 576, 32*9 = 288
    STRIP_H = 24                    # top menu strip height
    FRAME_MS = 16                   # ~60 fps

    def __init__(self, root):
        self.root = root
        root.title("cat's chip8emu")
        root.configure(bg=BG)
        root.resizable(False, False)

        self.cpu = Chip8()
        self.rom_data = None
        self.rom_name = ""
        self.running = False
        self.paused = False

        # settings (in memory only - FILES = OFF)
        self.speed_var = tk.StringVar(value="Normal (10)")
        self.color_var = tk.StringVar(value="Blue")
        self.cycles_per_frame = SPEEDS[self.speed_var.get()]
        self.pixel_on = PIXEL_COLORS[self.color_var.get()]

        # ---------------- chip8audio 0.1 ---------------------------------
        self.sound_var  = tk.BooleanVar(value=True)
        self.tone_var   = tk.StringVar(value="Mid (440 Hz)")
        self.volume_var = tk.StringVar(value="Medium")
        self.audio = Chip8Audio(
            root,
            freq=AUDIO_TONES[self.tone_var.get()],
            volume=AUDIO_VOLUMES[self.volume_var.get()],
        )
        root.protocol("WM_DELETE_WINDOW", self.exit_app)

        # ---------------- menu strip -------------------------------------
        self._build_menu()

        # ---------------- title ------------------------------------------
        tk.Label(
            root, text="C A T ' S   C H I P 8   E M U",
            bg=BG, fg=HUE, font=("Courier New", 14, "bold"),
        ).place(x=0, y=self.STRIP_H + 2, width=self.WIN_W, height=20)

        # ---------------- display ----------------------------------------
        cw = 64 * self.SCALE
        ch = 32 * self.SCALE
        border = 2
        cx = (self.WIN_W - (cw + 2 * border)) // 2
        cy = self.STRIP_H + 24

        self.canvas = tk.Canvas(
            root, width=cw, height=ch,
            bg=BG, bd=0,
            highlightthickness=border, highlightbackground=EDGE,
        )
        self.canvas.place(x=cx, y=cy)

        self.base_img = tk.PhotoImage(width=64, height=32)
        self._paint_base()
        self.zoom_img = self.base_img.zoom(self.SCALE)
        self.canvas_img_id = self.canvas.create_image(
            0, 0, anchor="nw", image=self.zoom_img
        )

        # ---------------- status -----------------------------------------
        sy = cy + ch + 2 * border + 4
        self.status = tk.Label(
            root, text="no rom loaded  -  use Load ROM in the menu",
            bg=BG, fg=HUE_DIM, font=("Courier New", 10),
        )
        self.status.place(x=0, y=sy, width=self.WIN_W, height=18)

        # ---------------- key hint ---------------------------------------
        tk.Label(
            root,
            text="keys: 1234/QWER/ASDF/ZXCV   P = pause   F5 = reset",
            bg=BG, fg=HUE_DIM, font=("Courier New", 9),
        ).place(x=0, y=self.WIN_H - 22, width=self.WIN_W, height=18)

        # ---------------- keyboard ---------------------------------------
        root.bind_all("<KeyPress>",   self.on_key_press)
        root.bind_all("<KeyRelease>", self.on_key_release)
        root.bind_all("<Control-o>",  lambda e: self.load_rom())
        root.bind_all("<F5>",         lambda e: self.reset_game())

        # center after the menu is attached so the size is right
        self._center_window(self.WIN_W, self.WIN_H)

        self.render()
        self.tick()

    # ------------------------------------------------------------------
    def _build_menu(self):
        # in-window menu strip at the very top (looks the same on every OS)
        # order: Load ROM | Help | About | Exit | Controls
        mopts = dict(
            bg=BG, fg=HUE,
            activebackground=BG_HOV, activeforeground=HUE_HI,
            selectcolor=HUE, tearoff=0, bd=1,
            font=("Courier New", 10),
        )

        strip = tk.Frame(self.root, bg=BG, bd=0,
                         highlightthickness=1, highlightbackground=EDGE)
        strip.place(x=0, y=0, width=self.WIN_W, height=self.STRIP_H)

        # Controls dropdown
        self.controls_menu = tk.Menu(self.root, **mopts)
        self.controls_menu.add_command(label="Key Mapping...",
                                       command=self.show_controls)
        self.controls_menu.add_separator()
        self.controls_menu.add_command(label="Pause / Resume   P",
                                       command=self.toggle_pause)
        self.controls_menu.add_command(label="Reset Game       F5",
                                       command=self.reset_game)
        self.controls_menu.add_separator()

        speed = tk.Menu(self.controls_menu, **mopts)
        for name in SPEEDS:
            speed.add_radiobutton(label=name, value=name,
                                  variable=self.speed_var,
                                  command=self._apply_settings)
        self.controls_menu.add_cascade(label="Speed", menu=speed)

        color = tk.Menu(self.controls_menu, **mopts)
        for name in PIXEL_COLORS:
            color.add_radiobutton(label=name, value=name,
                                  variable=self.color_var,
                                  command=self._apply_settings)
        self.controls_menu.add_cascade(label="Pixel Color", menu=color)

        # chip8audio 0.1 submenu
        audio = tk.Menu(self.controls_menu, **mopts)
        audio.add_checkbutton(label="Sound On       M",
                              variable=self.sound_var,
                              command=self._apply_audio)
        audio.add_separator()
        for name in AUDIO_TONES:
            audio.add_radiobutton(label=name, value=name,
                                  variable=self.tone_var,
                                  command=self._apply_audio)
        audio.add_separator()
        for name in AUDIO_VOLUMES:
            audio.add_radiobutton(label="Volume: " + name, value=name,
                                  variable=self.volume_var,
                                  command=self._apply_audio)
        audio.add_separator()
        audio.add_command(label="Test Beep", command=self._test_beep)
        self.controls_menu.add_cascade(label="Audio (chip8audio 0.1)",
                                       menu=audio)

        items = (
            ("Load ROM", self.load_rom),
            ("Help",     self.show_help),
            ("About",    self.show_about),
            ("Exit",     self.exit_app),
            ("Controls", None),
        )
        x = 2
        for text, cmd in items:
            w = 12 + len(text) * 9
            b = tk.Label(strip, text=text, bg=BG, fg=HUE,
                         font=("Courier New", 10, "bold"), cursor="hand2")
            b.place(x=x, y=1, width=w, height=self.STRIP_H - 4)
            b.bind("<Enter>", lambda e, w=b: w.config(bg=BG_HOV, fg=HUE_HI))
            b.bind("<Leave>", lambda e, w=b: w.config(bg=BG, fg=HUE))
            if cmd is None:
                b.bind("<Button-1>", lambda e, w=b: self._post_controls(w))
            else:
                b.bind("<ButtonRelease-1>", lambda e, c=cmd: c())
            x += w + 4

    def _post_controls(self, widget):
        x = widget.winfo_rootx()
        y = widget.winfo_rooty() + widget.winfo_height()
        try:
            self.controls_menu.tk_popup(x, y)
        finally:
            self.controls_menu.grab_release()

    # ------------------------------------------------------------------
    def _apply_settings(self):
        self.cycles_per_frame = SPEEDS[self.speed_var.get()]
        self.pixel_on = PIXEL_COLORS[self.color_var.get()]
        self.render()
        self.status.config(
            text="speed: %s   color: %s"
                 % (self.speed_var.get(), self.color_var.get()),
            fg=HUE)

    # ------------------------------------------------------------------
    def _apply_audio(self):
        self.audio.set_enabled(self.sound_var.get())
        self.audio.set_freq(AUDIO_TONES[self.tone_var.get()])
        self.audio.set_volume(AUDIO_VOLUMES[self.volume_var.get()])
        self.status.config(
            text="audio: %s  %s  vol %s  [%s]" % (
                "on" if self.sound_var.get() else "off",
                self.tone_var.get(), self.volume_var.get(),
                self.audio.backend),
            fg=HUE)

    def _toggle_sound(self):
        self.sound_var.set(not self.sound_var.get())
        self._apply_audio()

    def _test_beep(self):
        if not self.sound_var.get():
            self.status.config(text="audio is off  -  press M to turn on",
                               fg=HUE_HI)
            return
        self.audio.set_active(True)
        self.root.after(250, lambda: self.audio.set_active(
            self.running and not self.paused and self.cpu.sound_timer > 0))

    # ------------------------------------------------------------------
    def _center_window(self, w, h):
        self.root.update_idletasks()
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x = max(0, (sw - w) // 2)
        y = max(0, (sh - h) // 2)
        self.root.geometry("%dx%d+%d+%d" % (w, h, x, y))

    # ------------------------------------------------------------------
    def _make_button(self, parent, text, cmd):
        return tk.Button(
            parent, text=text, command=cmd,
            bg=BG, fg=HUE,
            activebackground=BG_HOV, activeforeground=HUE_HI,
            relief="flat", bd=0, overrelief="flat",
            highlightthickness=1,
            highlightbackground=EDGE, highlightcolor=EDGE,
            font=("Courier New", 11, "bold"),
            cursor="hand2", takefocus=0,
        )

    # ------------------------------------------------------------------
    def _paint_base(self):
        on = self.pixel_on
        rows = []
        for y in range(32):
            row = self.cpu.display[y]
            rows.append("{" + " ".join(on if p else BG for p in row) + "}")
        self.base_img.put(" ".join(rows))

    # ------------------------------------------------------------------
    def render(self):
        self._paint_base()
        self.zoom_img = self.base_img.zoom(self.SCALE)
        self.canvas.itemconfig(self.canvas_img_id, image=self.zoom_img)

    # ------------------------------------------------------------------
    def tick(self):
        if self.running and not self.paused:
            for _ in range(self.cycles_per_frame):
                self.cpu.cycle()

            # chip8audio: buzzer sounds while sound_timer > 0
            self.audio.set_active(self.cpu.sound_timer > 0)

            if self.cpu.delay_timer > 0:
                self.cpu.delay_timer -= 1
            if self.cpu.sound_timer > 0:
                self.cpu.sound_timer -= 1

            if self.cpu.draw_flag:
                self.render()
                self.cpu.draw_flag = False

        self.root.after(self.FRAME_MS, self.tick)

    # ------------------------------------------------------------------
    # menu actions
    # ------------------------------------------------------------------
    def load_rom(self):
        path = filedialog.askopenfilename(
            title="select a chip-8 ROM",
            filetypes=[
                ("Chip-8 ROMs", "*.ch8 *.c8 *.rom *.bin *.CH8 *.C8 *.ROM"),
                ("All files", "*.*"),
            ],
        )
        if not path:
            return
        try:
            with open(path, "rb") as f:     # read-only; FILES = OFF
                data = f.read()
        except OSError as e:
            self.status.config(text="error: %s" % e, fg=HUE_HI)
            return

        if not data:
            self.status.config(text="error: rom is empty", fg=HUE_HI)
            return

        self.rom_data = data
        self.rom_name = path.replace("\\", "/").split("/")[-1]
        self._load_into_memory()

        self.running = True
        self.paused = False
        self.status.config(
            text="loaded: %s  (%d bytes)  -  running" % (self.rom_name, len(data)),
            fg=HUE,
        )
        self.render()

    # ------------------------------------------------------------------
    def _load_into_memory(self):
        self.audio.silence()
        self.cpu.reset()
        for i, b in enumerate(self.rom_data):
            if 0x200 + i < 4096:
                self.cpu.memory[0x200 + i] = b
        self.cpu.pc = 0x200
        self.cpu.draw_flag = True

    # ------------------------------------------------------------------
    def toggle_pause(self):
        if self.rom_data is None:
            self.status.config(text="no rom loaded  -  use Load ROM first",
                               fg=HUE_HI)
            return
        if not self.running:
            self.reset_game()
            return
        self.paused = not self.paused
        if self.paused:
            self.audio.silence()
        self.status.config(
            text=("paused: %s" % self.rom_name) if self.paused
                 else ("running: %s" % self.rom_name),
            fg=HUE_DIM if self.paused else HUE,
        )

    # ------------------------------------------------------------------
    def reset_game(self):
        if self.rom_data is None:
            self.status.config(text="no rom loaded  -  use Load ROM first",
                               fg=HUE_HI)
            return
        self._load_into_memory()
        self.running = True
        self.paused = False
        self.status.config(text="reset: %s" % self.rom_name, fg=HUE)
        self.render()

    # ------------------------------------------------------------------
    def show_help(self):
        self._dialog(
            "help",
            [
                ("HOW TO PLAY", HUE, ("Courier New", 13, "bold")),
                ("Load ROM  pick a .ch8/.c8/.rom/.bin", HUE_DIM, None),
                ("Controls  keys, pause, reset, speed", HUE_DIM, None),
                ("Exit      close the emulator", HUE_DIM, None),
                ("", HUE, None),
                ("KEYPAD MAPPING", HUE, ("Courier New", 12, "bold")),
                ("1 2 3 C   ->   1 2 3 4", HUE_DIM, None),
                ("4 5 6 D   ->   Q W E R", HUE_DIM, None),
                ("7 8 9 E   ->   A S D F", HUE_DIM, None),
                ("A 0 B F   ->   Z X C V", HUE_DIM, None),
            ],
        )

    # ------------------------------------------------------------------
    def show_controls(self):
        self._dialog(
            "controls",
            [
                ("CONTROLS", HUE, ("Courier New", 13, "bold")),
                ("chip-8 key   ->   keyboard", HUE_DIM, None),
                ("1 2 3 C      ->   1 2 3 4", HUE_DIM, None),
                ("4 5 6 D      ->   Q W E R", HUE_DIM, None),
                ("7 8 9 E      ->   A S D F", HUE_DIM, None),
                ("A 0 B F      ->   Z X C V", HUE_DIM, None),
                ("", HUE, None),
                ("P        pause / resume", HUE_DIM, None),
                ("M        sound on / off", HUE_DIM, None),
                ("F5       reset game", HUE_DIM, None),
                ("Ctrl+O   load rom", HUE_DIM, None),
            ],
        )

    # ------------------------------------------------------------------
    def show_about(self):
        self._dialog(
            "about",
            [
                ("CAT'S CHIP8 EMU 0.1.1", HUE, ("Courier New", 13, "bold")),
                ("", HUE, None),
                ("a chip-8 virtual machine", HUE_DIM, None),
                ("written in python + tkinter", HUE_DIM, None),
                ("64 x 32 display, 16-key keypad", HUE_DIM, None),
                ("35 opcodes, 4096 bytes of ram", HUE_DIM, None),
                ("audio: " + self.audio.describe(), HUE_DIM, None),
                ("", HUE, None),
                ("single file. files = off.", HUE_DIM, None),
                ("(c) A.C Holdings / Team Flames", HUE, None),
            ],
        )

    # ------------------------------------------------------------------
    def _dialog(self, title, lines):
        w, h = 400, 320
        dlg = tk.Toplevel(self.root)
        dlg.title(title)
        dlg.configure(bg=BG)
        dlg.resizable(False, False)
        dlg.transient(self.root)

        self.root.update_idletasks()
        rx = self.root.winfo_rootx()
        ry = self.root.winfo_rooty()
        rw = self.root.winfo_width()
        rh = self.root.winfo_height()
        dlg.geometry("%dx%d+%d+%d" % (
            w, h, rx + (rw - w) // 2, ry + (rh - h) // 2))

        frame = tk.Frame(
            dlg, bg=BG, bd=0,
            highlightthickness=2, highlightbackground=EDGE,
        )
        frame.place(x=12, y=12, width=w - 24, height=h - 70)

        y = 8
        for text, color, font in lines:
            f = font if font else ("Courier New", 10)
            tk.Label(
                frame, text=text, bg=BG, fg=color, font=f, anchor="w",
            ).place(x=14, y=y, width=w - 56, height=22)
            y += 22

        btn = self._make_button(dlg, "CLOSE", dlg.destroy)
        btn.place(x=(w - 140) // 2, y=h - 48, width=140, height=36)

        dlg.bind("<Escape>", lambda e: dlg.destroy())
        dlg.grab_set()
        dlg.focus_set()

    # ------------------------------------------------------------------
    def exit_app(self):
        try:
            self.audio.close()
        finally:
            self.root.destroy()

    # ------------------------------------------------------------------
    def on_key_press(self, event):
        k = event.keysym.lower()
        if k in KEYMAP:
            self.cpu.keys[KEYMAP[k]] = 1
        elif k == "p":
            self.toggle_pause()
        elif k == "m":
            self._toggle_sound()

    def on_key_release(self, event):
        k = event.keysym.lower()
        if k in KEYMAP:
            self.cpu.keys[KEYMAP[k]] = 0


# ---------------------------------------------------------------------------
def main():
    root = tk.Tk()
    CatChip8Emu(root)
    root.mainloop()


if __name__ == "__main__":
    main()
