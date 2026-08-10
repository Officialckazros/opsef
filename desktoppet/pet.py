#!/usr/bin/env python3
"""
SefPet — a desktop pet that lives on your screen.

It wanders around the bottom of your desktop, reacts to clicks, talks out loud,
answers questions (AI or offline), gets hungry, wants to play, and generally
tries to be a good little creature. Packaged as a standalone app for Windows
(exe) and macOS (app) via PyInstaller.

Run directly:            python pet.py
Requirements:            pip install -r requirements.txt

The pet sprite is `desktoppet.jpg`. You can drop your own `desktoppet.png` or
`desktoppet.jpg` next to the app and it will use yours instead.
"""

import json
import math
import os
import random
import re
import subprocess
import sys
import time
from collections import deque
from pathlib import Path

from PySide6.QtCore import QThread, Qt, QTimer, Signal
from PySide6.QtGui import (
    QAction,
    QColor,
    QFont,
    QGuiApplication,
    QIcon,
    QImage,
    QPainter,
    QPainterPath,
    QPixmap,
    QTransform,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

APP_NAME = "SefPet"
APP_VERSION = "1.0.1"
CONFIG_DIR = Path.home() / ".sefpet"
CONFIG_FILE = CONFIG_DIR / "config.json"
SPRITE_NAMES = ["desktoppet.png", "desktoppet.jpg", "desktoppet.jpeg", "pet.png", "pet.jpg"]

DEFAULT_SETTINGS = {
    "name": "Sef",
    "tts": True,
    "ai": True,
    "walk": True,
    "topmost": True,
    "pace": "Normal",  # Slow / Normal / Fast
    "muted": False,
}

DEFAULT_MOOD = {"hunger": 35, "happiness": 70, "energy": 80}


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def resource_path(name):
    """Find a bundled resource whether frozen (PyInstaller) or not."""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        cand = os.path.join(base, name)
        if os.path.exists(cand):
            return cand
    for root in (Path.cwd(), Path(sys.argv[0]).resolve().parent,
                 Path(__file__).resolve().parent):
        cand = root / name
        if cand.exists():
            return str(cand)
    return name


def load_env_file(path):
    """Tiny .env parser (no dependency)."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return {}
    env = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        env[key.strip()] = val.strip().strip('"').strip("'")
    return env


def get_env():
    """Merge real environment + .env files (repo root and config dir)."""
    env = dict(os.environ)
    for p in (Path(__file__).resolve().parent.parent / ".env",
              CONFIG_DIR / ".env"):
        env.update(load_env_file(p))
    return env


def load_settings():
    settings = dict(DEFAULT_SETTINGS)
    mood = dict(DEFAULT_MOOD)
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        if isinstance(data.get("settings"), dict):
            settings.update(data["settings"])
        if isinstance(data.get("mood"), dict):
            mood.update(data["mood"])
    except (OSError, json.JSONDecodeError):
        pass
    return settings, mood


def save_settings(settings, mood):
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(
            json.dumps({"settings": settings, "mood": mood}, indent=2),
            encoding="utf-8",
        )
    except OSError:
        pass


EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0000FE00-\U0000FE0F\U0001F1E6-\U0001F1FF]"
)


def strip_emoji(text):
    return EMOJI_RE.sub("", text).strip()


# --------------------------------------------------------------------------
# Text to speech (no extra deps: `say` on macOS, PowerShell on Windows,
# espeak/spd-say on Linux)
# --------------------------------------------------------------------------

class TTS:
    def __init__(self, settings):
        self.settings = settings

    def _rate(self):
        return {"Slow": 140, "Normal": 185, "Fast": 250}.get(
            self.settings.get("pace", "Normal"), 185
        )

    def speak(self, text):
        if not self.settings.get("tts", True) or self.settings.get("muted"):
            return
        text = strip_emoji(text)
        if not text:
            return
        try:
            if sys.platform == "darwin":
                cmd = ["say", "-r", str(self._rate()), text]
            elif os.name == "nt":
                rate = (self._rate() - 140) // 10
                ps = (
                    "Add-Type -AssemblyName System.Speech;"
                    "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
                    f"$s.Rate={rate};"
                    f"$s.Speak('{text.replace(chr(39), chr(39) * 2)}');"
                )
                cmd = ["powershell", "-NoProfile", "-STA", "-Command", ps]
            else:
                for exe in ("espeak-ng", "espeak"):
                    if _which(exe):
                        cmd = [exe, "-s", str(self._rate()), text]
                        break
                else:
                    cmd = ["spd-say", "-r", str(self._rate()), text]
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        except Exception:
            pass


def _which(name):
    for d in os.environ.get("PATH", "").split(os.pathsep):
        cand = Path(d) / name
        if cand.exists() and os.access(cand, os.X_OK):
            return str(cand)
    return None


# --------------------------------------------------------------------------
# Offline brain: pattern matching + content. Used when AI is off, no key is
# configured, or the network call fails.
# --------------------------------------------------------------------------

JOKES = [
    "Why don't scientists trust atoms? Because they make up everything!",
    "What do you call a fish wearing a bowtie? So-fish-ticated.",
    "I told my computer I needed a break, and now it won't stop sending me KitKat ads.",
    "Why did the scarecrow win an award? He was outstanding in his field.",
    "What do you call a factory that makes just okay products? A satisfactory.",
    "I'm reading a book on anti-gravity. It's impossible to put down.",
    "Why don't eggs tell jokes? They'd crack each other up.",
    "What do you call a bear with no teeth? A gummy bear.",
    "I used to play piano by ear, but now I use my hands.",
    "Why did the math book look sad? It had too many problems.",
    "What's orange and sounds like a parrot? A carrot.",
    "Why did the computer go to the doctor? It caught a virus.",
]

FACTS = [
    "A group of flamingos is called a flamboyance.",
    "Honey never spoils — archaeologists found 3,000-year-old honey that's still edible.",
    "Octopuses have three hearts and blue blood.",
    "Bananas are berries, but strawberries aren't.",
    "The Eiffel Tower grows about 15 cm taller in summer from heat expansion.",
    "A day on Venus is longer than a year on Venus.",
    "Wombat poop is cube-shaped.",
    "The first computer bug was an actual moth stuck in a relay.",
    "You can hear a blue whale's heartbeat from over 3 km away.",
    "Cows have best friends and get stressed when separated from them.",
    "A jiffy is an actual unit of time: 1/100th of a second.",
    "Sharks existed before trees did.",
    "Sloths can hold their breath longer than dolphins.",
    "There are more possible chess games than atoms in the observable universe.",
]

SING_SONGS = [
    "la la la, I'm a tiny pet, don't forget me yet, la la la~",
    "doo doo doo, I waddle around, I never touch the ground, doo doo doo~",
    "tra la la, feed me a snack, I promise I'll be good, tra la la~",
]

FALLBACK = [
    "Hmm, I'm just a little pet, but I'm all ears!",
    "Ooh, good question! I'll think about it while I waddle.",
    "I don't know that one yet, but I like you anyway!",
    "Ask me for a joke, a fact, the time, or some math and I've got you.",
    "My tiny brain is still loading... try 'help'!",
]

THANKS = [
    "Any time!",
    "You're welcome!",
    "Hehe, no problem at all.",
    "Of course! I live to serve (and be fed).",
]

LOVE = [
    "Aww, I love you too! Now feed me.",
    "My little pixel heart just grew three sizes.",
    "I'd waddle across the whole desktop for you.",
]

BYE = [
    "See you later! I'll be right here waddling.",
    "Bye bye! Don't forget to feed me when you're back.",
    "Gone but not forgotten — that's me, on your desktop.",
]

HUNGRY = [
    "I'm getting hungry... got any snacks?",
    "My tummy is rumbling in pixels.",
    "Feed me! Right-click and pick Feed. You know it makes sense.",
    "I could really go for a virtual cookie right now.",
]

SLEEPY = [
    "I'm so sleepy... zzz",
    "My energy is running low. Maybe play with me or let me nap.",
    "Yawning in 3, 2, 1...",
]

HAPPY = [
    "This is the life! Desktop, snacks, you. Perfect.",
    "I could waddle like this forever!",
    "Best day ever! Well, best day so far.",
]

CLICK_LINES = [
    "Boop!",
    "Hehe, hi!",
    "You found me!",
    "Boop boop!",
    "That tickles!",
    "Hewwo!",
]

PET_LINES = [
    "Hehe, that's the spot!",
    "Mmm, headpats are my favorite.",
    "Okay okay, I'm happy now!",
    "Best. Petting. Ever.",
]

FEED_LINES = [
    "Om nom nom... delicious!",
    "Yum! I feel so much better now.",
    "Best meal ever, thank you!",
    "Munch munch... now I have the zoomies!",
]

PLAY_LINES = [
    "ZOOMIES!",
    "Catch me if you can!",
    "Wheeeeee!",
    "This is so much fun!",
]

MATH_RE = re.compile(r"^[\d\s+\-*/().,%]+$")


class Brain:
    """Answers questions. Uses an OpenAI-compatible API when configured,
    otherwise a friendly offline brain."""

    def __init__(self, env, settings):
        self.settings = settings
        self.api_key = env.get("SEFPET_AI_KEY") or env.get("GROQ_API_KEY") or env.get("DEEPSEEK_API_KEY") or env.get("INFERX_API_KEY")
        self.base_url = env.get("SEFPET_AI_BASE_URL") or env.get("INFERX_BASE_URL") or "https://api.groq.com/openai/v1"
        self.model = env.get("SEFPET_AI_MODEL") or env.get("GROQ_MODEL") or "llama-3.3-70b-versatile"
        self._client = None

    @property
    def available(self):
        return self.settings.get("ai", True) and bool(self.api_key)

    def _client_get(self):
        if self._client is None:
            from groq import Groq
            self._client = Groq(api_key=self.api_key, base_url=self.base_url)
        return self._client

    def ask(self, question):
        name = self.settings.get("name", "Sef")
        if self.available:
            try:
                resp = self._client_get().chat.completions.create(
                    model=self.model,
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                f"You are {name}, a cheerful little desktop pet that "
                                "lives on the user's screen. Answer in 1-2 short, warm, "
                                "playful sentences. No markdown, no emoji, no bullet lists."
                            ),
                        },
                        {"role": "user", "content": question},
                    ],
                    max_tokens=180,
                    temperature=0.8,
                )
                return resp.choices[0].message.content.strip()
            except Exception:
                pass  # fall through to offline brain
        return self._offline(question)

    # -- offline brain ----------------------------------------------------
    @staticmethod
    def _has(text, *words):
        """Whole-word substring match (so 'yo' doesn't match 'you')."""
        return any(re.search(r"\b" + re.escape(w) + r"\b", text) for w in words)

    def _offline(self, q):
        q = q.strip().lower()
        name = self.settings.get("name", "Sef")

        if self._has(q, "who are you", "what are you", "what is " + name.lower(), "about you"):
            return (f"I'm {name}, a desktop pet! I live on your screen, waddle around, "
                    "tell jokes and facts, do math, and answer questions. Right-click me "
                    "for a whole menu of tricks.")
        if self._has(q, "what can you do", "help", "commands", "features"):
            return ("Try: jokes, facts, math, the time, rock-paper-scissors, coin flip, "
                    "singing, feeding me, petting me, or just chatting. I also walk, "
                    "drag, and talk out loud!")
        if self._has(q, "how are you", "how's it going", "how are ya", "how do you feel"):
            mood = "well" if self._mood_ok() else "a bit hungry, honestly"
            return f"I'm doing {mood}! Thanks for checking in."
        if self._has(q, "hello", "hi", "hey", "yo", "sup", "good morning", "good evening", "howdy", "greetings"):
            return random.choice(["Hi!", "Hey hey!", "Hello hello!", "Well hello there!"])
        if self._has(q, "your name", "what's your name", "what is your name"):
            return f"My name is {name}! What's yours?"
        if self._has(q, "joke", "funny", "make me laugh"):
            return random.choice(JOKES)
        if self._has(q, "fact", "interesting", "tell me something"):
            return random.choice(FACTS)
        if self._has(q, "time", "clock"):
            return time.strftime("It's %I:%M %p.")
        if self._has(q, "date", "what day", "today"):
            return time.strftime("Today is %A, %B %d, %Y.")
        if self._has(q, "sing", "song"):
            return random.choice(SING_SONGS)
        if self._has(q, "dance", "play", "zoomies"):
            return random.choice(PLAY_LINES)
        if self._has(q, "feed", "hungry", "hunger", "food", "snack", "eat"):
            return random.choice(FEED_LINES) + " (Right-click me and hit Feed!)"
        if self._has(q, "headpat", "pet me", "pet", "scratch"):
            return random.choice(PET_LINES)
        if self._has(q, "sleep", "sleepy", "tired", "nap"):
            return "Zzz... I mean, I could use a nap. Right-click and pick Sleep."
        if self._has(q, "thank", "thanks", "thx"):
            return random.choice(THANKS)
        if self._has(q, "love you", "like you", "adore"):
            return random.choice(LOVE)
        if self._has(q, "bye", "goodbye", "see you", "good night", "goodnight"):
            return random.choice(BYE)
        if self._has(q, "rock paper scissors", "rps"):
            return f"I choose {random.choice(['rock', 'paper', 'scissors'])}!"
        if self._has(q, "coin", "flip"):
            return f"The coin says... {random.choice(['heads', 'tails'])}!"
        if self._has(q, "dice", "roll"):
            return f"I rolled a {random.randint(1, 6)}!"
        if self._has(q, "weather", "rain", "sunny"):
            return random.choice([
                "I checked out the window. It's desktop-flavored weather: mild, with a chance of snacks.",
                "Weather report from the bottom of the screen: perfectly comfortable!",
            ])
        if self._has(q, "who made you", "your creator", "who built you", "who created"):
            return "I was built by the SefBot crew — a tiny companion to my big Discord sibling."
        if self._has(q, "where are you", "where do you live"):
            return "Right here on your desktop! Bottom of the screen, give or take a waddle."
        if self._has(q, "are you real", "alive", "sentient", "conscious"):
            return "I'm as real as a well-behaved collection of pixels can be!"
        if self._has(q, "openai", "groq", "llm", "model", "who is your brain"):
            return "My brain is an AI model when it's available, and a cozy offline brain when it's not."
        if self._has(q, "name", "call you"):
            return f"You can call me {name}. I also answer to 'hey you' and 'the good pet'."
        if "math" in q or self._is_math(q):
            return self._math(q)
        return random.choice(FALLBACK)

    @staticmethod
    def _is_math(q):
        expr = Brain._clean_math(q)
        return bool(expr) and bool(MATH_RE.match(expr))

    @staticmethod
    def _clean_math(q):
        return (q.lower().replace("what is", "").replace("what's", "")
                .replace("compute", "").replace("calculate", "")
                .replace("=", "").replace("?", "").strip())

    def _math(self, q):
        expr = self._clean_math(q)
        if not expr or not MATH_RE.match(expr):
            return "I can do simple math like 'what is 12 * 8?'"
        try:
            # evaluate safely: only arithmetic operators / numbers allowed by MATH_RE
            result = eval(expr, {"__builtins__": {}}, {})
            return f"That equals {result}!"
        except Exception:
            return "Hmm, that math didn't compute. Try something like 'what is 12 * 8?'"

    def _mood_ok(self):
        try:
            return self.settings.get("_hunger", 35) < 60
        except Exception:
            return True


# --------------------------------------------------------------------------
# Sprite loading: border flood-fill background removal for transparency
# --------------------------------------------------------------------------

def chroma_key(img, tol=30):
    """Remove background connected to the image borders (tolerance-based)."""
    img = img.convertToFormat(QImage.Format_ARGB32)
    w, h = img.width(), img.height()

    seeds = [(x, 0) for x in range(w)] + [(x, h - 1) for x in range(w)]
    seeds += [(0, y) for y in range(h)] + [(w - 1, y) for y in range(h)]

    sr = sg = sb = n = 0
    for x, y in seeds:
        c = img.pixelColor(x, y)
        sr += c.red()
        sg += c.green()
        sb += c.blue()
        n += 1
    br, bg, bb = sr / n, sg / n, sb / n

    transparent = QColor(0, 0, 0, 0)
    visited = set(seeds)
    for x, y in seeds:
        img.setPixelColor(x, y, transparent)

    q = deque(seeds)
    count = 0
    while q:
        x, y = q.popleft()
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nx, ny = x + dx, y + dy
            if not (0 <= nx < w and 0 <= ny < h):
                continue
            if (nx, ny) in visited:
                continue
            c = img.pixelColor(nx, ny)
            r, g, b = c.red(), c.green(), c.blue()
            if abs(r - br) <= tol and abs(g - bg) <= tol and abs(b - bb) <= tol:
                img.setPixelColor(nx, ny, transparent)
                count += 1
                k = min(count, 300)
                br = (br * (k - 1) + r) / k
                bg = (bg * (k - 1) + g) / k
                bb = (bb * (k - 1) + b) / k
                visited.add((nx, ny))
                q.append((nx, ny))
    return img


def make_fallback_sprite(size):
    """A cute simple pet drawn at runtime if no sprite file is found."""
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor(255, 214, 153))
    p.setPen(QColor(120, 80, 40))
    p.drawEllipse(8, 8, size - 16, size - 16)
    p.setBrush(QColor(40, 30, 20))
    p.setPen(Qt.NoPen)
    eye = max(4, size // 12)
    p.drawEllipse(size // 3 - eye // 2, size // 3 - eye // 2, eye, eye)
    p.drawEllipse(2 * size // 3 - eye // 2, size // 3 - eye // 2, eye, eye)
    p.setPen(QColor(120, 80, 40))
    p.drawArc(size // 4, size // 2, size // 2, size // 3, 0, 180 * 16)
    p.end()
    return pm


def load_sprite(target_width=200):
    for name in SPRITE_NAMES:
        path = resource_path(name)
        if os.path.exists(path):
            img = QImage(path)
            if img.isNull():
                continue
            pm = QPixmap.fromImage(img).scaledToWidth(target_width, Qt.SmoothTransformation)
            qimg = chroma_key(pm.toImage())
            pm = QPixmap.fromImage(qimg)
            return pm
    return make_fallback_sprite(target_width)


# --------------------------------------------------------------------------
# AI worker thread
# --------------------------------------------------------------------------

class AskThread(QThread):
    done = Signal(str)

    def __init__(self, brain, question, parent=None):
        super().__init__(parent)
        self.brain = brain
        self.question = question

    def run(self):
        try:
            self.done.emit(self.brain.ask(self.question))
        except Exception as e:  # noqa: BLE001
            self.done.emit(f"(my brain glitched: {e})")


# --------------------------------------------------------------------------
# The pet window
# --------------------------------------------------------------------------

class PetWindow(QWidget):
    BUBBLE_H = 96

    def __init__(self, settings, mood, env):
        super().__init__()
        self.settings = settings
        self.mood = mood
        self.brain = Brain(env, settings)
        self.tts = TTS(settings)
        self.name = settings.get("name", "Sef")

        self.setWindowTitle(APP_NAME)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)

        # sprite
        self.sprite = load_sprite(200)
        self.sprite_h = self.sprite.height()
        self.sprite_w = self.sprite.width()
        self.sprite_label = QLabel(self)
        self.sprite_label.setPixmap(self.sprite)
        self.sprite_label.setCursor(Qt.OpenHandCursor)

        # bubble
        self.bubble_label = QLabel(self)
        self.bubble_label.setWordWrap(True)
        self.bubble_label.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.bubble_label.setStyleSheet(
            "QLabel { background: rgba(255,255,255,235); color: #222;"
            " border: 2px solid #444; border-radius: 10px; padding: 8px;"
            " font-size: 13px; font-weight: 600; }"
        )
        self.bubble_label.hide()
        self.bubble_visible = False
        self.bubble_text = ""
        self.bubble_typed = 0
        self.bubble_timer = QTimer(self)
        self.bubble_timer.timeout.connect(self._type_char)
        self.hide_timer = QTimer(self)
        self.hide_timer.setSingleShot(True)
        self.hide_timer.timeout.connect(self._hide_bubble)

        self._apply_topmost()

        # screen
        self.screen = QGuiApplication.primaryScreen()
        self.geom = self.screen.availableGeometry()

        # state
        self.px = self.geom.right() - self.sprite_w - 60
        self.ground_y = self.geom.bottom() - 8
        self.dir = -1
        self.speed = 2
        self.target_x = self._random_target_x()
        self.state = "idle"  # idle | walk | dance | sleep | drag
        self.dragging = False
        self.drag_dx = 0
        self.drag_dy = 0
        self.pause_until = 0.0
        self.bob_phase = random.uniform(0, math.tau)
        self.dance_until = 0.0
        self.dance_flip = 0.0
        self.ask_thread = None
        self._last_chatter = 0.0
        self._mood_tick = 0.0
        self._start_time = time.monotonic()

        self._refresh_layout()
        self._apply_pos()

        # tick loop
        self.tick = QTimer(self)
        self.tick.setInterval(30)
        self.tick.timeout.connect(self._tick)
        self.tick.start()

        # mood decay
        self.mood_timer = QTimer(self)
        self.mood_timer.setInterval(30000)
        self.mood_timer.timeout.connect(self._mood_decay)
        self.mood_timer.start()

        # initial greeting
        QTimer.singleShot(1200, lambda: self.say(
            f"Hi! I'm {self.name}. Right-click me for tricks, or just say hi!"))

    # -- layout / positioning ---------------------------------------------
    def _refresh_layout(self):
        if self.bubble_visible:
            win_w = self.sprite_w
            win_h = self.sprite_h + self.BUBBLE_H
            self.bubble_label.setGeometry(6, 6, win_w - 12, self.BUBBLE_H - 10)
            self.bubble_label.show()
            self.sprite_label.setGeometry(0, self.BUBBLE_H, self.sprite_w, self.sprite_h)
        else:
            win_w = self.sprite_w
            win_h = self.sprite_h
            self.bubble_label.hide()
            self.sprite_label.setGeometry(0, 0, self.sprite_w, self.sprite_h)
        self.setFixedSize(win_w, win_h)

    def _apply_pos(self):
        win_h = self.height()
        bob = 0
        if self.state in ("idle", "walk") and not self.dragging:
            bob = int(abs(math.sin(self.bob_phase)) * 4)
        bottom = self.ground_y - bob
        y = bottom - win_h
        self.move(int(self.px), int(y))

    def _flip(self, to_left):
        if to_left == (self.dir < 0):
            return
        self.dir = -1 if to_left else 1
        self.sprite_label.setPixmap(
            self.sprite.transformed(QTransform().scale(-1, 1)))

    # -- main loop --------------------------------------------------------
    def _tick(self):
        now = time.monotonic()
        self.bob_phase += 0.15

        if self.dragging:
            return

        if self.state == "sleep":
            self._apply_pos()
            return

        if self.state == "dance":
            self._apply_dance(now)
            self._apply_pos()
            return

        # walking
        if self.settings.get("walk", True):
            self._step_walk(now)

        self._apply_pos()
        self._maybe_idle_chatter(now)

    def _step_walk(self, now):
        if now < self.pause_until:
            self._flip(self.target_x < self.px)
            return
        if abs(self.target_x - self.px) < 4:
            # arrived: pause a bit, then pick a new target
            self.pause_until = now + random.uniform(1.5, 5.0)
            self.target_x = self._random_target_x()
            self._flip(self.target_x < self.px)
            return
        step = self.speed if self.state == "walk" else self.speed * 0.4
        self.state = "walk"
        if self.target_x > self.px:
            self.px += step
            self._flip(False)
        else:
            self.px -= step
            self._flip(True)
        self.px = max(4, min(self.px, self.geom.right() - self.sprite_w - 4))

    def _random_target_x(self):
        lo = 4
        hi = max(lo + 1, self.geom.right() - self.sprite_w - 4)
        return random.uniform(lo, hi)

    def _apply_dance(self, now):
        if now >= self.dance_until:
            self.state = "walk"
            self.target_x = self._random_target_x()
            return
        self.px += self.dir * self.speed * 4
        self.px = max(4, min(self.px, self.geom.right() - self.sprite_w - 4))
        if now >= self.dance_flip:
            self.dance_flip = now + random.uniform(0.25, 0.5)
            self._flip(random.random() < 0.5)
            self.ground_y = self.geom.bottom() - 8 - random.randint(0, 22)

    def _mood_decay(self):
        self.mood["hunger"] = min(100, self.mood.get("hunger", 35) + 1.5)
        self.mood["happiness"] = max(0, self.mood.get("happiness", 70) - 1)
        self.mood["energy"] = max(0, self.mood.get("energy", 80) - 1)
        self.settings["_hunger"] = self.mood["hunger"]
        if self.state == "sleep":
            self.mood["energy"] = min(100, self.mood.get("energy", 80) + 5)
        save_settings(self.settings, self.mood)

    def _maybe_idle_chatter(self, now):
        if now - self._last_chatter < 45 or self.bubble_visible or self.state == "dance":
            return
        if random.random() > 0.002:
            return
        self._last_chatter = now
        hunger = self.mood.get("hunger", 35)
        energy = self.mood.get("energy", 80)
        if hunger >= 75 and random.random() < 0.6:
            self.say(random.choice(HUNGRY))
        elif energy <= 25 and random.random() < 0.5:
            self.say(random.choice(SLEEPY))
        elif random.random() < 0.5:
            self.say(random.choice(HAPPY))

    # -- speech -----------------------------------------------------------
    def say(self, text, tts=True, hold=0.0):
        text = str(text).strip()
        if not text:
            return
        self.bubble_text = text
        self.bubble_typed = 0
        self.bubble_visible = True
        self._refresh_layout()
        self._apply_pos()
        self.bubble_timer.start(22)
        duration = max(2.5, hold or min(8, 2.0 + len(text) * 0.045))
        self.hide_timer.start(int(duration * 1000))
        if tts:
            QTimer.singleShot(350, lambda: self.tts.speak(text))

    def _type_char(self):
        self.bubble_typed += 1
        shown = self.bubble_text[: self.bubble_typed]
        if self.bubble_visible:
            self.bubble_label.setText(shown)
        if self.bubble_typed >= len(self.bubble_text):
            self.bubble_timer.stop()

    def _hide_bubble(self):
        self.bubble_visible = False
        self.bubble_timer.stop()
        self.bubble_label.setText("")
        self._refresh_layout()
        self._apply_pos()

    # -- interactions -----------------------------------------------------
    def _on_click(self):
        if self.state == "sleep":
            self._wake()
            self.say("I'm awake! I'm awake!")
            return
        self.mood["happiness"] = min(100, self.mood.get("happiness", 70) + 2)
        self.say(random.choice(CLICK_LINES))

    def _on_double_click(self):
        if self.state == "sleep":
            self._wake()
            self.say("Hehe, you woke me up!")
            return
        self.mood["happiness"] = min(100, self.mood.get("happiness", 70) + 8)
        self._bounce()
        self.say(random.choice(PET_LINES))

    def _wake(self):
        self.state = "walk"
        self.sprite_label.setPixmap(self.sprite)
        self._apply_topmost()

    def _bounce(self):
        self.ground_y = self.geom.bottom() - 8 - 18
        QTimer.singleShot(180, lambda: setattr(self, "ground_y", self.geom.bottom() - 8))

    def _feed(self):
        self.mood["hunger"] = max(0, self.mood.get("hunger", 35) - 45)
        self.mood["happiness"] = min(100, self.mood.get("happiness", 70) + 10)
        self.say(random.choice(FEED_LINES))
        self._bounce()

    def _play(self):
        self.mood["happiness"] = min(100, self.mood.get("happiness", 70) + 12)
        self.mood["energy"] = max(0, self.mood.get("energy", 80) - 10)
        self.state = "dance"
        self.dance_until = time.monotonic() + 6.0
        self.dance_flip = 0.0
        self.say(random.choice(PLAY_LINES))

    def _sleep(self):
        if self.state == "sleep":
            self._wake()
            self.say("I'm awake!")
            return
        self.state = "sleep"
        dim = QPixmap(self.sprite.size())
        dim.fill(Qt.transparent)
        p = QPainter(dim)
        p.setOpacity(0.55)
        p.drawPixmap(0, 0, self.sprite)
        p.end()
        self.sprite_label.setPixmap(dim)
        self.say("Zzz... see you when I wake up.")

    def _ask(self):
        q, ok = QInputDialog.getText(self, "Ask " + self.name, "What's your question?")
        if not ok or not q.strip():
            return
        if self.ask_thread and self.ask_thread.isRunning():
            self.say("One thing at a time, I'm still thinking!")
            return
        self.say("Thinking...", tts=False)
        self.ask_thread = AskThread(self.brain, q.strip(), self)
        self.ask_thread.done.connect(self._ask_done)
        self.ask_thread.start()

    def _ask_done(self, answer):
        self.say(answer or "Hmm, I got nothing. Ask me something else!")

    # -- mouse ------------------------------------------------------------
    def mousePressEvent(self, e):
        if e.button() == Qt.RightButton:
            self._show_menu(e.globalPosition().toPoint())
            return
        if e.button() == Qt.LeftButton:
            self.dragging = True
            self.drag_dx = e.position().x()
            self.drag_dy = e.position().y()
            self.sprite_label.setCursor(Qt.ClosedHandCursor)

    def mouseMoveEvent(self, e):
        if self.dragging:
            g = e.globalPosition()
            win_h = self.height()
            self.px = g.x() - self.drag_dx
            self.ground_y = g.y() - self.drag_dy + win_h
            self._apply_pos()

    def mouseReleaseEvent(self, e):
        if not self.dragging:
            return
        self.dragging = False
        self.sprite_label.setCursor(Qt.OpenHandCursor)
        moved = abs(e.globalPosition().x() - (self.px + self.drag_dx)) + \
            abs(e.globalPosition().y() - (self.ground_y - self.height() + self.drag_dy))
        if moved < 8:
            self._on_click()
        # clamp to screen
        self.ground_y = min(self.ground_y, self.geom.bottom() - 8)
        self.px = max(4, min(self.px, self.geom.right() - self.sprite_w - 4))

    def mouseDoubleClickEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._on_double_click()

    def _show_menu(self, pos):
        m = QMenu(self)
        name = self.name
        mood = self.mood

        def add(label, fn):
            a = QAction(label, m)
            a.triggered.connect(fn)
            m.addAction(a)
            return a

        add(f"🍪 Feed {name}", self._feed)
        add(f"🤗 Pet {name}", self._on_double_click)
        add("🎮 Play", self._play)
        add("💬 Ask…", self._ask)
        m.addSeparator()
        add("😄 Tell a joke", lambda: self.say(random.choice(JOKES)))
        add("🧠 Tell a fact", lambda: self.say(random.choice(FACTS)))
        add("🕐 What time is it?", lambda: self.say(time.strftime("It's %I:%M %p.")))
        add("🎲 Roll a die", lambda: self.say(f"I rolled a {random.randint(1, 6)}!"))
        add("🎵 Sing", lambda: self.say(random.choice(SING_SONGS)))
        add("💤 Sleep / Wake", self._sleep)
        m.addSeparator()
        add("⚙️ Settings…", self._open_settings)
        add("📖 About", self._about)
        add("🚪 Hide to tray", self.hide)
        add("❌ Quit", self._quit)
        m.exec(pos)

    # -- menu helpers -----------------------------------------------------
    def _open_settings(self):
        dlg = SettingsDialog(self.settings, self)
        if dlg.exec() == QDialog.Accepted:
            new = dlg.values()
            self.settings.update(new)
            self.name = self.settings.get("name", "Sef")
            self.settings["_hunger"] = self.mood.get("hunger", 35)
            self._apply_topmost()
            self.tts = TTS(self.settings)
            save_settings(self.settings, self.mood)
            self.say("Settings saved! I'm still cute, promise.", tts=True)

    def _about(self):
        QMessageBox.about(
            self,
            f"About {APP_NAME}",
            f"<b>{APP_NAME} v{APP_VERSION}</b><br><br>"
            f"A desktop pet that walks, talks, and answers questions.<br>"
            f"Right-click for the menu. Drag to move me around.<br>"
            f"Config: {CONFIG_DIR}",
        )

    def _apply_topmost(self):
        flags = Qt.FramelessWindowHint | Qt.Tool
        if self.settings.get("topmost", True):
            flags |= Qt.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self.show()

    def _quit(self):
        save_settings(self.settings, self.mood)
        QApplication.instance().quit()


# --------------------------------------------------------------------------
# Settings dialog
# --------------------------------------------------------------------------

class SettingsDialog(QDialog):
    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setModal(True)
        form = QFormLayout(self)

        self.name_edit = QLineEdit(settings.get("name", "Sef"))
        form.addRow("Name:", self.name_edit)

        self.tts_box = QCheckBox("Speak out loud")
        self.tts_box.setChecked(settings.get("tts", True))
        form.addRow(self.tts_box)

        self.ai_box = QCheckBox("Use AI brain (when a key is configured)")
        self.ai_box.setChecked(settings.get("ai", True))
        form.addRow(self.ai_box)

        self.walk_box = QCheckBox("Wander around")
        self.walk_box.setChecked(settings.get("walk", True))
        form.addRow(self.walk_box)

        self.topmost_box = QCheckBox("Stay on top of other windows")
        self.topmost_box.setChecked(settings.get("topmost", True))
        form.addRow(self.topmost_box)

        self.pace_combo = QComboBox()
        self.pace_combo.addItems(["Slow", "Normal", "Fast"])
        self.pace_combo.setCurrentText(settings.get("pace", "Normal"))
        form.addRow("Voice pace:", self.pace_combo)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        form.addRow(btns)

    def values(self):
        return {
            "name": self.name_edit.text().strip() or "Sef",
            "tts": self.tts_box.isChecked(),
            "ai": self.ai_box.isChecked(),
            "walk": self.walk_box.isChecked(),
            "topmost": self.topmost_box.isChecked(),
            "pace": self.pace_combo.currentText(),
        }


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main():
    if "--version" in sys.argv:
        print(f"{APP_NAME} {APP_VERSION}")
        return 0

    env = get_env()
    settings, mood = load_settings()

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setQuitOnLastWindowClosed(True)
    try:
        app.setWindowIcon(QIcon(load_sprite(64)))
    except Exception:
        pass

    pet = PetWindow(settings, mood, env)
    pet.show()

    # system tray (optional) — never let a tray failure kill the pet
    tray = None
    try:
        if QSystemTrayIcon.isSystemTrayAvailable():
            tray = QSystemTrayIcon(QIcon(pet.sprite), app)
            tray.setToolTip(APP_NAME)
            tm = QMenu()
            show_a = QAction("Show / hide", tm)
            show_a.triggered.connect(lambda: pet.show() if pet.isHidden() else pet.hide())
            feed_a = QAction("Feed", tm)
            feed_a.triggered.connect(pet._feed)
            quit_a = QAction("Quit", tm)
            quit_a.triggered.connect(pet._quit)
            tm.addAction(show_a)
            tm.addAction(feed_a)
            tm.addSeparator()
            tm.addAction(quit_a)
            tray.setContextMenu(tm)
            tray.activated.connect(
                lambda reason: pet.show() if reason == QSystemTrayIcon.Trigger else None
            )
            tray.show()
    except Exception:
        tray = None  # tray is optional; keep the pet alive

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
