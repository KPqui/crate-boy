"""
Crate Boy DESK: hold a key, talk into the desk mic, let go. The clip is transcribed locally
and added to today's Desk session.

The key listener only reacts to the one chosen key. Nothing else you type is recorded or stored.
"""
import collections
import threading
import time
import wave
from pathlib import Path

import numpy as np

SR_OUT = 16000


def parts_available():
    try:
        import sounddevice  # noqa: F401
        import pynput  # noqa: F401
        return True, ""
    except Exception as ex:  # missing, or PortAudio problem
        return False, str(ex)


def key_name(key):
    """pynput key -> stable text name ('ctrl_r', 'pause', 'a', 'f9')."""
    n = getattr(key, "name", None)
    if n:
        return n
    ch = getattr(key, "char", None)
    if ch:
        return ch.lower()
    vk = getattr(key, "vk", None)
    return f"vk{vk}" if vk is not None else str(key)


def resample(x, sr_in, sr_out=SR_OUT):
    """Low-pass + linear interpolation. Plenty for speech."""
    if sr_in == sr_out or len(x) == 0:
        return x.astype(np.float32)
    if sr_in > sr_out:
        cutoff = 0.45 * sr_out / sr_in
        n = 63
        t = np.arange(n) - (n - 1) / 2
        h = np.sinc(2 * cutoff * t) * np.hamming(n)
        h /= h.sum()
        x = np.convolve(x, h, mode="same")
    t_out = np.arange(int(len(x) * sr_out / sr_in)) * (sr_in / sr_out)
    return np.interp(t_out, np.arange(len(x)), x).astype(np.float32)


def beep(kind):
    try:
        import winsound
        f, d = {"start": (1320, 60), "stop": (880, 60), "short": (300, 120)}[kind]
        threading.Thread(target=winsound.Beep, args=(f, d), daemon=True).start()
    except Exception:
        pass


class Desk:
    def __init__(self, clip_dir, on_clip, log):
        self.clip_dir = Path(clip_dir)
        self.clip_dir.mkdir(parents=True, exist_ok=True)
        self.on_clip = on_clip          # called with (wav_path, start_time_str)
        self.log = log
        self.on = False
        self.rec = False
        self.level = 0.0
        self.peak = 0.0
        self.err = ""
        self.capture = False
        self.cfg = {}
        self.sr = 48000
        self._stream = None
        self._listener = None
        self._buf = []
        self._pre = collections.deque()
        self._pre_len = 0
        self._t0 = 0.0
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- devices
    @staticmethod
    def devices():
        import sounddevice as sd
        apis = sd.query_hostapis()
        out = []
        for i, d in enumerate(sd.query_devices()):
            if d["max_input_channels"] > 0:
                out.append({"index": i, "name": d["name"], "api": apis[d["hostapi"]]["name"],
                            "channels": d["max_input_channels"], "sr": int(d["default_samplerate"])})
        return out

    @staticmethod
    def default_device(devs):
        def score(d):
            s = 0
            if "focusrite" in d["name"].lower() or "scarlett" in d["name"].lower():
                s += 10
            if "analog" in d["name"].lower() or "analogue" in d["name"].lower():
                s += 2
            if d["api"] == "MME":
                s += 3                       # most compatible while Ableton uses ASIO
            elif d["api"].startswith("Windows WASAPI"):
                s += 2
            return s
        return max(devs, key=score)["index"] if devs else None

    # ---------------------------------------------------------------- start / stop
    def start(self, cfg):
        import sounddevice as sd
        from pynput import keyboard
        self.stop()
        self.cfg = dict(cfg)
        dev = sd.query_devices(self.cfg["device"])
        self.sr = int(dev["default_samplerate"])
        ch = int(self.cfg.get("channel", 1))
        nch = min(max(ch, 1), dev["max_input_channels"])
        self._ch = nch - 1
        self._pre_len = int(0.35 * self.sr)         # keep the first syllable
        self._stream = sd.InputStream(device=self.cfg["device"], channels=nch, samplerate=self.sr,
                                      dtype="float32", blocksize=0, callback=self._audio)
        self._stream.start()
        self._listener = keyboard.Listener(on_press=self._press, on_release=self._release)
        self._listener.daemon = True
        self._listener.start()
        self.on, self.err = True, ""
        self.log("desk on", dev["name"], "ch", nch, "key", self.cfg.get("key"))

    def stop(self):
        self.on = False
        self.rec = False
        for obj, fn in ((self._stream, "close"), (self._listener, "stop")):
            try:
                if obj is not None:
                    getattr(obj, fn)()
            except Exception:
                pass
        self._stream = self._listener = None
        self.level = 0.0

    # ---------------------------------------------------------------- callbacks
    def _audio(self, data, frames, t, status):
        x = data[:, self._ch] * (10 ** (float(self.cfg.get("gain_db", 0)) / 20))
        rms = float(np.sqrt(np.mean(x * x))) if len(x) else 0.0
        self.level = 0.8 * self.level + 0.2 * rms
        self.peak = max(self.peak * 0.97, float(np.max(np.abs(x))) if len(x) else 0.0)
        with self._lock:
            if self.rec:
                self._buf.append(x)
            else:
                self._pre.append(x)
                total = sum(len(b) for b in self._pre)
                while self._pre and total - len(self._pre[0]) >= self._pre_len:
                    total -= len(self._pre.popleft())

    def _press(self, key):
        k = key_name(key)
        if self.capture:
            self.cfg["key"] = k
            self.capture = False
            self.log("desk key set", k)
            return
        if k == self.cfg.get("key") and not self.rec and self.on:
            with self._lock:
                self._buf = list(self._pre)
                self._pre.clear()
                self.rec = True
            self._t0 = time.time()
            beep("start")

    def _release(self, key):
        if key_name(key) != self.cfg.get("key") or not self.rec:
            return
        with self._lock:
            self.rec = False
            buf, self._buf = self._buf, []
        dur = time.time() - self._t0
        if dur < 0.6 or not buf:
            beep("short")                       # too short: ignored (accidental tap)
            return
        beep("stop")
        audio = resample(np.concatenate(buf), self.sr)
        pk = float(np.max(np.abs(audio))) if len(audio) else 0.0
        if pk > 0.99:                       # boost pushed it over: scale down instead of clipping
            audio = audio * (0.99 / pk)
        stamp = time.strftime("%H:%M:%S", time.localtime(self._t0))
        path = self.clip_dir / (time.strftime("%Y%m%d-%H%M%S", time.localtime(self._t0)) + ".wav")
        write_wav(path, audio)
        threading.Thread(target=self.on_clip, args=(path, stamp), daemon=True).start()


def db(v):
    return round(20 * np.log10(v), 1) if v > 1e-6 else -120.0


def normalize(x, target=0.9, max_gain_db=40):
    """Bring quiet clips up before transcription (peak to -1 dB, at most +40 dB)."""
    p = float(np.max(np.abs(x))) if len(x) else 0.0
    if p < 1e-5:
        return x
    g = min(target / p, 10 ** (max_gain_db / 20))
    return np.clip(x * g, -1, 1).astype(np.float32)


def write_wav(path, audio):
    pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR_OUT)
        w.writeframes(pcm.tobytes())


def read_wav(path):
    with wave.open(str(path), "rb") as w:
        x = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
    return x.astype(np.float32) / 32768.0
