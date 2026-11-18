"""
Crate Boy - local server. Opens the UI in its own window, runs only on this PC (127.0.0.1).
"""
import csv
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse, unquote

APP = Path(__file__).resolve().parent
BASE = APP.parent
IN = BASE / "IN"
OUT = BASE / "OUT"
DATA = OUT / ".crateboy"
SESS = DATA / "sessions"
for d in (IN, OUT, DATA, SESS):
    d.mkdir(parents=True, exist_ok=True)

# pythonw has no console: send all output to a log file
_log = open(DATA / "log.txt", "a", encoding="utf-8", buffering=1)
if sys.stdout is None or not sys.stdout.isatty():
    sys.stdout = sys.stderr = _log

sys.path.insert(0, str(APP))
import engine as E  # noqa: E402
import sync as G  # noqa: E402

PHONE = OUT / "phone"
PHONE.mkdir(parents=True, exist_ok=True)
LEGEND_FILE = DATA / "legend.json"


def migrate_old_installs():
    """First start of a v2 install: take over sessions, notes, clips and IN files from old CrateBoy_v1.x folders."""
    flag = DATA / "migrated.txt"
    if flag.exists():
        return
    found = []
    for root in {BASE.parent, BASE.parent.parent}:
        for pat in ("CrateBoy", "*/CrateBoy", "*/*/CrateBoy"):
            try:
                for d in root.glob(pat):
                    if d.resolve() != BASE.resolve() and (d / "OUT" / ".crateboy").is_dir():
                        found.append(d)
            except OSError:
                pass
    found = sorted(set(found), key=lambda d: (d / "OUT" / ".crateboy").stat().st_mtime)
    import shutil as _sh
    for old in found:                                   # oldest first, newer ones win for single files
        od = old / "OUT" / ".crateboy"
        for f in (od / "sessions").glob("*.json"):
            if not (SESS / f.name).exists():
                _sh.copy2(f, SESS / f.name)
        for name in ("inbox.json", "history.csv", "pending.json", "settings.json"):
            if (od / name).exists():
                _sh.copy2(od / name, DATA / name)
        for sub in ("desk",):
            if (old / "OUT" / sub).is_dir():
                (OUT / sub).mkdir(exist_ok=True)
                for f in (old / "OUT" / sub).iterdir():
                    if not (OUT / sub / f.name).exists():
                        _sh.copy2(f, OUT / sub / f.name)
        for f in list((old / "OUT").glob("*.txt")) + list((old / "OUT").glob("*.csv")) + list((old / "OUT").glob("*.xml")):
            if not (OUT / f.name).exists():
                _sh.copy2(f, OUT / f.name)
        for f in (old / "IN").glob("*"):
            if f.is_file() and not f.name.startswith(".") and not (IN / f.name).exists():
                _sh.copy2(f, IN / f.name)
    flag.write_text("from: " + ", ".join(str(d) for d in found) if found else "nothing to take over", encoding="utf-8")
    if found:
        print("migrated from", [str(d) for d in found], flush=True)


migrate_old_installs()
E.load_private(LEGEND_FILE)

PORT = 8818
VERSION = (Path(__file__).resolve().parent / "VERSION").read_text().strip() if (Path(__file__).resolve().parent / "VERSION").exists() else "dev"
AUDIO = {".m4a", ".mp3", ".wav", ".aac", ".ogg", ".flac", ".3gp", ".amr", ".opus", ".wma", ".mp4"}
MODELS = {"accurate": "large-v3", "fast": "small", "turbo": "large-v3-turbo"}
LEVELS = ["CRATE ROOKIE", "DIGGER", "SELECTOR", "CRATE KEEPER", "SELECTA", "RECORD SAGE", "CRATE WIZARD"]

LOCK = threading.RLock()
STATE = {"lib": None, "lib_error": "", "health": None, "job": {"state": "idle"}, "ping": time.time()}


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


# ---------------------------------------------------------------- library
def library(reload=False):
    with LOCK:
        p = E.find_library(IN)
        lib = STATE["lib"]
        if p is None:
            STATE.update(lib=None, health=None, lib_error="")
            return None
        if lib is None or reload or lib.path != p or lib.mtime != p.stat().st_mtime:
            try:
                lib = E.Library(p)
                STATE.update(lib=lib, health=lib.health(), lib_error="")
                log("library loaded", p.name, len(lib.tracks))
            except Exception as ex:  # broken export
                STATE.update(lib=None, health=None, lib_error=f"Could not read {p.name}: {ex}")
                return None
        return STATE["lib"]


# ---------------------------------------------------------------- sessions / cards
def settings():
    return E.load_json(DATA / "settings.json", {"model": "accurate", "language": "en", "sound": True})


def new_card(text, t=""):
    d = E.draft_from_note(text)
    c = {"id": uuid.uuid4().hex[:6], "t": t, "text": text, "status": "new", "exported": False,
         "source": "draft", **d, "match": None, "cands": []}
    rematch(c)
    return c


def rematch(c, force=False):
    lib = library()
    if lib is None:
        return
    if c.get("match") and not force:
        return
    if not (c.get("title") or c.get("artist")):
        return
    c["cands"] = lib.search(c.get("title", ""), c.get("artist", ""))
    sure = E.Library.sure(c["cands"])
    c["match"] = sure


def cards_from_text(text, t=""):
    return [new_card(n, t) for n in E.split_notes(text)]


def make_session(name, cards, source, notes=None):
    s = {"id": time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4], "name": name,
         "created": dt.datetime.now().isoformat(timespec="minutes"), "source": source, "cards": cards,
         "notes": notes if notes is not None else [[c.get("t", ""), c["text"]] for c in cards]}
    E.save_json(SESS / f"{s['id']}.json", s)
    return s


def get_session(sid):
    if sid == "pending":
        cards = E.load_json(DATA / "pending.json", [])
        s = {"id": "pending", "name": "PENDING (not in crate yet)", "created": "", "source": "pending", "cards": cards}
    else:
        s = E.load_json(SESS / f"{safe(sid)}.json", None)
    if s is None:
        return None
    for c in s["cards"]:
        if not c.get("match") and c.get("status") != "skip":
            rematch(c)
    return s


def session_notes(s):
    """The raw transcript lines of a session (older sessions: rebuilt from the cards, deduplicated)."""
    if s.get("notes"):
        return s["notes"]
    f = OUT / f"{s.get('name', '')}_notes.txt"          # sessions from before v1.1: use the saved transcript
    if f.exists():
        out = []
        for line in f.read_text(encoding="utf-8").splitlines():
            m = re.match(r"\[(.*?)\]\s*(.*)", line)
            if m:
                out.append([m.group(1), m.group(2)])
        if out:
            return out
    seen, out = set(), []
    for c in s["cards"]:
        for t, txt in c.get("said") or [[c.get("t", ""), c["text"]]]:
            if (t, txt) not in seen:
                seen.add((t, txt)); out.append([t, txt])
    return out


def rebuild_from_claude(s, tracks):
    """Replace the unburned cards with Claude's per-track rows."""
    notes = session_notes(s)
    s["notes"] = notes
    keep = [c for c in s["cards"] if c.get("exported")]
    new = []
    for tr in tracks:
        said = [[t, txt] for t, txt in notes if t in tr["times"]]
        c = {"id": uuid.uuid4().hex[:6], "t": tr["times"][0] if tr["times"] else "",
             "text": " / ".join(txt for _, txt in said) or "(no matching lines)", "said": said,
             "note": tr["note"], "status": "new", "exported": False, "source": "claude",
             "title": tr["title"], "artist": tr["artist"], "rating": tr["rating"], "tags": tr["tags"],
             "genre_spec": tr["genre_spec"], "match": None, "cands": []}
        rematch(c, force=True)
        new.append(c)
    s["cards"] = keep + new
    return len(new)


def save_session(s):
    if s["id"] == "pending":
        E.save_json(DATA / "pending.json", s["cards"])
    else:
        E.save_json(SESS / f"{safe(s['id'])}.json", s)


def list_sessions():
    out = []
    for p in sorted(SESS.glob("*.json"), reverse=True):
        s = E.load_json(p, None)
        if not s:
            continue
        cs = s["cards"]
        out.append({"id": s["id"], "name": s["name"], "created": s["created"], "n": len(cs),
                    "done": sum(c["status"] != "new" for c in cs),
                    "ready": sum(c["status"] == "keep" and bool(c.get("match")) and not c["exported"] for c in cs),
                    "topend": sum((c["status"] == "pending" or (c["status"] == "keep" and not c.get("match")))
                                  and not c["exported"] for c in cs)})
    pend = E.load_json(DATA / "pending.json", [])
    if pend:
        out.insert(0, {"id": "pending", "name": "PENDING (not in crate yet)", "created": "", "n": len(pend),
                       "done": sum(c["status"] != "new" for c in pend),
                       "ready": sum(c["status"] == "keep" and bool(c.get("match")) for c in pend), "topend": 0})
    return out


def safe(name):
    return re.sub(r"[^\w.\- ()]", "_", Path(name).name)


# ---------------------------------------------------------------- xp
def xp():
    rows = []
    p = DATA / "history.csv"
    if p.exists():
        with open(p, encoding="utf-8", newline="") as f:
            rows = list(csv.reader(f))
    n = len(rows)
    days = sorted({r[0] for r in rows if r}, reverse=True)
    streak, d = 0, dt.date.today()
    for day in days:
        if day == d.isoformat():
            streak += 1
            d -= dt.timedelta(days=1)
        elif streak == 0 and day == (d - dt.timedelta(days=1)).isoformat():
            d -= dt.timedelta(days=1)
            streak += 1
            d -= dt.timedelta(days=1)
        else:
            break
    level = n // 25
    return {"xp": n, "level": level + 1, "title": LEVELS[min(level, len(LEVELS) - 1)],
            "into": n % 25, "need": 25, "streak": streak,
            "today": sum(1 for r in rows if r and r[0] == E.today())}


# ---------------------------------------------------------------- transcription job
def job_set(**kw):
    with LOCK:
        STATE["job"] = {**STATE["job"], **kw}


_MODEL = {"name": None, "obj": None}
_MODEL_LOCK = threading.Lock()


def get_model(model_key):
    from faster_whisper import WhisperModel
    name = MODELS.get(model_key, "large-v3")
    with _MODEL_LOCK:
        if _MODEL["name"] != name:
            _MODEL.update(name=name, obj=WhisperModel(name, device="cpu", compute_type="int8"))
        return _MODEL["obj"]


def transcribe_job(fname, model_key, language):
    src = IN / safe(fname)
    try:
        job_set(state="loading", file=fname, progress=0, msg="Loading the speech model...", session=None)
        fake = os.environ.get("CRATEBOY_FAKE_ASR")
        notes = []
        if fake:
            for line in Path(fake).read_text(encoding="utf-8").splitlines():
                m = re.match(r"\[(.*?)\]\s*(.*)", line)
                if m:
                    notes.append([m.group(1), m.group(2)])
                    time.sleep(0.2)
                    job_set(state="running", progress=min(99, len(notes) * 10), msg=f"{len(notes)} notes found")
        else:
            try:
                import faster_whisper  # noqa: F401
            except ImportError:
                raise RuntimeError("The speech engine is not installed. Close Crate Boy and start CrateBoy.bat again.")
            name = MODELS.get(model_key, "large-v3")
            job_set(msg=f"Loading model {name} (the first time it downloads once, 0.5-3 GB)...")
            model = get_model(model_key)
            job_set(state="running", msg="Reading the recording...")
            audio = load_audio(src)
            job_set(msg="Listening for your voice...")
            segs, info = model.transcribe(
                audio, language=None if language == "auto" else language,
                vad_filter=True, vad_parameters={"min_silence_duration_ms": 1500, "threshold": 0.6},
                initial_prompt=E.VOCAB or None, condition_on_previous_text=False, beam_size=5)
            dur = max(1.0, info.duration)
            last_end = -99.0
            for s in segs:
                txt = s.text.strip()
                if txt and not E.is_hallucination(txt):
                    if notes and s.start - last_end < 1.2 and len(notes[-1][1]) < 240:
                        notes[-1][1] += " " + txt
                    else:
                        notes.append([fmt_t(s.start), txt])
                    last_end = s.end
                job_set(progress=int(100 * s.end / dur), msg=f"{len(notes)} notes found")
        cards = []
        for t, txt in notes:
            cards += cards_from_text(txt, t)
        s = make_session(Path(fname).stem, cards, "voice", notes)
        (OUT / f"{Path(fname).stem}_notes.txt").write_text(
            "\n".join(f"[{t}] {x}" for t, x in notes), encoding="utf-8")
        job_set(state="done", progress=100, msg=f"{len(cards)} tracks ready to sort", session=s["id"])
        log("transcribed", fname, len(cards))
    except Exception as ex:
        log("transcribe failed", traceback.format_exc())
        job_set(state="error", msg=str(ex))


def load_audio(path, sr=16000):
    """Decode any audio file to 16 kHz mono float32 ourselves.
    (faster-whisper's own loader passes an option newer PyAV versions no longer accept.)"""
    import av
    import numpy as np
    chunks = []
    with av.open(str(path)) as container:
        stream = container.streams.audio[0]
        res = av.AudioResampler(format="s16", layout="mono", rate=sr)
        for frame in container.decode(stream):
            for f in res.resample(frame):
                chunks.append(f.to_ndarray().reshape(-1))
        for f in res.resample(None):
            chunks.append(f.to_ndarray().reshape(-1))
    if not chunks:
        raise RuntimeError("No audio found in that file.")
    return np.concatenate(chunks).astype(np.float32) / 32768.0


def fmt_t(t):
    t = int(t)
    return f"{t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}"




# ---------------------------------------------------------------- desk (hold-to-talk)
import desk as D  # noqa: E402
import numpy as np  # noqa: E402

SESS_LOCK = threading.RLock()
DESK = D.Desk(OUT / "desk", lambda p, t: desk_clip(p, t), log)
DESK_STATE = {"busy": 0, "install": "", "last": ""}


INBOX = DATA / "inbox.json"


def inbox():
    return E.load_json(INBOX, [])


class _Quiet(Exception):
    pass


def add_inbox(item):
    with SESS_LOCK:
        items = inbox()
        items.append(item)
        E.save_json(INBOX, items)
    DESK_STATE["last"] = item["text"]
    log("desk note", item["t"], item["text"][:60])


def desk_clip(path, stamp):
    DESK_STATE["busy"] += 1
    try:
        fake = os.environ.get("CRATEBOY_FAKE_DESK")
        if fake:
            text = fake
        else:
            st = settings()
            model = get_model(st.get("model", "accurate"))
            lang = st.get("language", "en")
            raw = D.read_wav(path)
            if D.db(float(np.max(np.abs(raw))) if len(raw) else 0) < -60:
                text = "(too quiet - turn up BOOST or the interface gain)"
                raise _Quiet()
            audio = D.normalize(raw)
            segs, _ = model.transcribe(audio, language=None if lang == "auto" else lang,
                                       vad_filter=False, initial_prompt=E.VOCAB or None,
                                       condition_on_previous_text=False, beam_size=5)
            text = " ".join(x.text.strip() for x in segs if not E.is_hallucination(x.text)).strip()
        item = {"id": uuid.uuid4().hex[:8], "day": time.strftime("%Y-%m-%d"), "t": stamp,
                "text": text or "(heard nothing)", "wav": Path(path).name, "used": False}
        add_inbox(item)
    except _Quiet:
        add_inbox({"id": uuid.uuid4().hex[:8], "day": time.strftime("%Y-%m-%d"), "t": stamp,
                   "text": "(too quiet - turn up BOOST or the interface gain)", "wav": Path(path).name, "used": False})
    except Exception:
        log("desk clip failed", traceback.format_exc())
        DESK_STATE["last"] = "(transcription failed - see log)"
    finally:
        DESK_STATE["busy"] -= 1


def desk_cfg():
    st = settings()
    return st.get("desk", {})


def desk_start(cfg):
    ok, why = D.parts_available()
    if not ok:
        raise RuntimeError("Desk parts are not installed yet.")
    cfg = {**desk_cfg(), **cfg}
    if cfg.get("device") is None:
        cfg["device"] = D.Desk.default_device(D.Desk.devices())
    cfg.setdefault("channel", 1)
    cfg.setdefault("key", "ctrl_r")
    DESK.start(cfg)
    st = settings(); st["desk"] = cfg; st["desk_auto"] = True; E.save_json(DATA / "settings.json", st)
    threading.Thread(target=get_model, args=(st.get("model", "accurate"),), daemon=True).start()  # warm up


def desk_install():
    py = Path(sys.executable)
    if py.name.lower() == "pythonw.exe" and py.with_name("python.exe").exists():
        py = py.with_name("python.exe")
    DESK_STATE["install"] = "installing"
    try:
        flags = 0x08000000 if os.name == "nt" else 0          # no console window
        r = subprocess.run([str(py), "-m", "pip", "install", "sounddevice", "pynput"],
                           capture_output=True, text=True, creationflags=flags)
        log("desk install", r.returncode, r.stdout[-400:], r.stderr[-400:])
        DESK_STATE["install"] = "done" if r.returncode == 0 else "failed: " + (r.stderr.strip().splitlines() or ["?"])[-1]
    except Exception as ex:
        DESK_STATE["install"] = f"failed: {ex}"


def desk_state():
    if DESK.on and not DESK.capture and DESK.cfg.get("key") and DESK.cfg.get("key") != desk_cfg().get("key"):
        st = settings(); st.setdefault("desk", {}).update(DESK.cfg); E.save_json(DATA / "settings.json", st)
    ok, why = D.parts_available()
    s = None
    out = {"available": ok, "why": why, "on": DESK.on, "rec": DESK.rec, "level": round(DESK.level, 4),
           "peak": round(DESK.peak, 4), "cfg": DESK.cfg if DESK.on else desk_cfg(), "capture": DESK.capture,
           "busy": DESK_STATE["busy"], "install": DESK_STATE["install"], "last": DESK_STATE["last"],
           "err": DESK.err, "rms_db": D.db(DESK.level), "peak_db": D.db(DESK.peak),
           "inbox_n": sum(not i["used"] for i in inbox()), "devices": []}
    if ok:
        try:
            out["devices"] = D.Desk.devices()
            if out["cfg"].get("device") is None:
                out["cfg"] = {**out["cfg"], "device": D.Desk.default_device(out["devices"])}
        except Exception as ex:
            out["why"] = str(ex)
    return out


# ---------------------------------------------------------------- phone link (private GitHub repo)
LINK = {"state": "off", "msg": "", "last": "", "pulled": 0, "busy": 0, "legend_sha": None}
SEEN_FILE = DATA / "phone_seen.json"
PHONE_Q = []
PHONE_LOCK = threading.Lock()


def link_cfg():
    return settings().get("github", {})


def make_link():
    c = link_cfg()
    if not (c.get("owner") and c.get("repo") and c.get("token")):
        return None
    return G.Link(c["owner"], c["repo"], c["token"], log)


def phone_item(audio_path, meta):
    seen = set(E.load_json(SEEN_FILE, []))
    if audio_path.name in seen:
        return
    seen.add(audio_path.name)
    E.save_json(SEEN_FILE, sorted(seen)[-5000:])
    with PHONE_LOCK:
        PHONE_Q.append((audio_path, meta))


def phone_worker():
    while True:
        time.sleep(1)
        with PHONE_LOCK:
            job = PHONE_Q.pop(0) if PHONE_Q else None
        if not job:
            continue
        LINK["busy"] += 1
        try:
            phone_process(*job)
        except Exception:
            log("phone item failed", traceback.format_exc())
        finally:
            LINK["busy"] -= 1


def _local(iso):
    try:
        return dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
    except Exception:
        return dt.datetime.now()


def phone_process(audio_path, meta):
    st = settings()
    lang = st.get("language", "en")
    if os.environ.get("CRATEBOY_FAKE_DESK"):
        segs = [(0.0, 3.0, os.environ["CRATEBOY_FAKE_DESK"])]
    else:
        model = get_model(st.get("model", "accurate"))
        audio = D.normalize(load_audio(audio_path))
        res, _ = model.transcribe(audio, language=None if lang == "auto" else lang,
                                  vad_filter=meta.get("kind") == "set",
                                  vad_parameters={"min_silence_duration_ms": 1500, "threshold": 0.6},
                                  initial_prompt=E.VOCAB or None, condition_on_previous_text=False, beam_size=5)
        segs = [(x.start, x.end, x.text.strip()) for x in res if x.text.strip() and not E.is_hallucination(x.text)]
    when = _local(meta.get("recordedAt") or meta.get("partStarted") or "")
    if meta.get("kind") == "set":
        offset = float(meta.get("offsetSec", 0))
        notes = []
        last_end = -99.0
        for a, b, txt in segs:
            if notes and a - last_end < 1.2 and len(notes[-1][2]) < 240:
                notes[-1][2] += " " + txt
            else:
                notes.append([offset + a, offset + b, txt])
            last_end = b
        sid = "phone-" + safe(str(meta.get("set", "set")))
        with SESS_LOCK:
            s = E.load_json(SESS / f"{sid}.json", None)
            if s is None:
                started = _local(meta.get("setStarted") or "")
                s = {"id": sid, "name": (meta.get("name") or "Phone set") + started.strftime(" %d.%m %H:%M"),
                     "created": started.isoformat(timespec="minutes"), "source": "phone", "cards": [], "notes": []}
            for a, b, txt in notes:
                t = fmt_t(a)
                s["notes"].append([t, txt])
                s["cards"] += cards_from_text(txt, t)
            s["notes"].sort(key=lambda n: [int(x) for x in n[0].split(":")])
            s.setdefault("parts", []).append(meta.get("part"))
            save_session(s)
        log("phone set part", sid, meta.get("part"), len(notes))
    else:
        text = " ".join(t for _, _, t in segs).strip() or "(heard nothing)"
        add_inbox({"id": uuid.uuid4().hex[:8], "day": when.strftime("%Y-%m-%d"), "t": when.strftime("%H:%M:%S"),
                   "text": text, "wav": audio_path.name, "src": "phone", "used": False})


def link_round():
    link = make_link()
    if link is None:
        LINK.update(state="off", msg="Not connected")
        return
    try:
        sha, how = link.pull_legend(LEGEND_FILE, LINK["legend_sha"])
        if sha and sha != LINK["legend_sha"]:
            LINK["legend_sha"] = sha
            E.load_private(LEGEND_FILE)
        n = G.pull_inbox(link, PHONE, phone_item, log)
        LINK.update(state="ok", msg=("legend: " + how) if not sha else "connected",
                    last=time.strftime("%H:%M:%S"), pulled=LINK["pulled"] + n)
    except G.GitHubError as ex:
        LINK.update(state="error", msg=str(ex), last=time.strftime("%H:%M:%S"))
    except Exception as ex:
        log("link round failed", traceback.format_exc())
        LINK.update(state="error", msg=f"{type(ex).__name__}: {ex}", last=time.strftime("%H:%M:%S"))


def link_loop():
    while True:
        link_round()
        for _ in range(60):
            time.sleep(1)
            if LINK.get("now"):
                LINK["now"] = False
                break


# ---------------------------------------------------------------- export
def export(fixes):
    lib = library()
    if lib is None:
        raise RuntimeError("No collection loaded. Export your collection from Rekordbox into IN first.")
    updates, hist, pend_new, descs = [], [], [], []
    pending = E.load_json(DATA / "pending.json", [])
    keep_pending = []
    sessions = [E.load_json(p, None) for p in SESS.glob("*.json")]
    sources = [(s, s["cards"]) for s in sessions if s] + [(None, pending)]
    for s, cards in sources:
        for c in cards:
            if s is None:                        # card from the pending pile
                if c["status"] == "keep" and c.get("match"):
                    pass
                else:
                    if c["status"] != "skip":
                        keep_pending.append(c)
                    continue
            elif c.get("exported") or c["status"] not in ("keep", "pending"):
                continue
            if c["status"] == "keep" and c.get("match"):
                m = c["match"]
                t = lib.by_id.get(m["trackid"])
                if t is None:
                    continue
                comment = E.format_tags(c["tags"])
                genre = E.apply_genre(t.get("Genre", ""), c.get("genre_spec", "")) if c.get("genre_spec") else None
                updates.append({"trackid": m["trackid"], "rating": c.get("rating"),
                                "comment": comment or None, "genre": genre})
                hist.append([E.today(), m["trackid"], t.get("Artist", ""), t.get("Name", ""),
                             c.get("rating") if c.get("rating") is not None else "", comment])
                said = " / ".join(x for _, x in (c.get("said") or [[c.get("t", ""), c.get("text", "")]]))
                descs.append([E.today(), m["trackid"], t.get("Artist", ""), t.get("Name", ""),
                              c.get("rating") if c.get("rating") is not None else "", comment, said])
            else:
                pc = dict(c, status="new", exported=False, match=None, cands=[])
                pend_new.append(pc)
            c["exported"] = True
        if s is not None:
            save_session(s)
    for f in fixes:
        updates.append({"trackid": f["trackid"], "rating": None, "comment": f["comment"], "genre": None})
    if not updates and not pend_new:
        return {"n": 0, "pending": 0, "path": ""}
    path = ""
    n = 0
    if updates:
        stamp = time.strftime("%Y%m%d-%H%M")
        path = OUT / f"crateboy_import_{stamp}.xml"
        n = E.build_import_xml(lib, updates, path, f"Crate Boy {stamp}")
    E.save_json(DATA / "pending.json", keep_pending + pend_new)
    if hist:
        with open(DATA / "history.csv", "a", encoding="utf-8", newline="") as f:
            csv.writer(f).writerows(hist)
    if descs:                       # KP's own words per track, kept for later use
        p = OUT / "descriptions.csv"
        new_file = not p.exists()
        with open(p, "a", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            if new_file:
                w.writerow(["date", "trackid", "artist", "title", "rating", "tags", "you said"])
            w.writerows(descs)
    log("export", n, "tracks", len(pend_new), "pending")
    return {"n": n, "pending": len(pend_new), "path": str(path), "file": Path(path).name if path else ""}


# ---------------------------------------------------------------- http
class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, obj, code=200, ctype="application/json"):
        body = obj if isinstance(obj, bytes) else json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path in ("/", "/index.html"):
                return self.send((APP / "ui.html").read_bytes(), ctype="text/html; charset=utf-8")
            if u.path == "/api/version":
                return self.send({"version": VERSION})
            if u.path == "/api/state":
                STATE["ping"] = time.time()
                lib = library()
                h = STATE["health"]
                files = []
                for p in sorted(IN.iterdir(), key=lambda p: -p.stat().st_mtime):
                    if p.is_file() and not p.name.startswith("."):
                        ext = p.suffix.lower()
                        kind = ("audio" if ext in AUDIO else "notes" if ext == ".txt"
                                else "crate" if lib and p == lib.path else "xml" if ext == ".xml" else "other")
                        files.append({"name": p.name, "kind": kind, "mb": round(p.stat().st_size / 1e6, 1)})
                return self.send({
                    "library": {"loaded": lib is not None, "name": lib.path.name if lib else "",
                                "stats": h[0] if h else None, "error": STATE["lib_error"]},
                    "files": files, "sessions": list_sessions(), "job": STATE["job"], "xp": xp(),
                    "settings": {k: v for k, v in settings().items() if k != "github"}, "legend": E.LEGEND,
                    "short": E.SHORT, "version": VERSION, "legend_loaded": E.PRIVATE["loaded"]})
            if u.path == "/api/desk":
                STATE["ping"] = time.time()
                return self.send(desk_state())
            if u.path == "/api/inbox":
                return self.send(inbox()[-1000:])
            if u.path == "/api/link":
                c = link_cfg()
                return self.send({**{k: v for k, v in LINK.items() if k != "now"}, "owner": c.get("owner", ""),
                                  "repo": c.get("repo", "crate-boy-data"), "has_token": bool(c.get("token")),
                                  "queue": len(PHONE_Q), "legend": E.PRIVATE["loaded"]})
            if u.path == "/api/clip":
                f = (PHONE if q.get("src") == "phone" else OUT / "desk") / safe(q.get("name", ""))
                if not f.exists():
                    return self.send({"error": "clip not found"}, 404)
                return self.send(f.read_bytes(), ctype="audio/wav" if f.suffix == ".wav" else "audio/webm")
            if u.path == "/api/session":
                s = get_session(q.get("id", ""))
                return self.send(s if s else {"error": "not found"}, 200 if s else 404)
            if u.path == "/api/prompt":
                s = get_session(q.get("id", ""))
                if s["id"] == "pending":
                    cards = [c for c in s["cards"] if c["status"] != "skip"]
                    return self.send({"text": E.claude_prompt(cards)})
                return self.send({"text": E.claude_prompt_full(session_notes(s))})
            if u.path == "/api/search":
                lib = library()
                return self.send(lib.search(q.get("q", ""), q.get("a", ""), 8) if lib else [])
            if u.path == "/api/check":
                library()
                h = STATE["health"]
                if not h:
                    return self.send({"stats": None, "issues": []})
                return self.send({"stats": h[0], "issues": h[1][:3000], "total_issues": len(h[1])})
            if u.path == "/api/format":
                return self.send({"comment": E.format_tags(json.loads(q.get("tags", "{}")))})
            self.send({"error": "not found"}, 404)
        except Exception as ex:
            log("GET error", traceback.format_exc())
            self.send({"error": str(ex)}, 500)

    def do_POST(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path == "/api/upload":
                name = safe(unquote(q.get("name", "upload.bin")))
                data = self.body()
                (IN / name).write_bytes(data)
                library()
                return self.send({"ok": True, "name": name})
            d = json.loads(self.body() or b"{}")
            if u.path == "/api/desk/start":
                try:
                    desk_start(d)
                except Exception as ex:
                    DESK.err = str(ex)
                    log("desk start failed", traceback.format_exc())
                    return self.send({"error": f"Mic could not start: {ex}"}, 500)
                return self.send(desk_state())
            if u.path == "/api/desk/stop":
                DESK.stop()
                st = settings(); st["desk_auto"] = False; E.save_json(DATA / "settings.json", st)
                return self.send(desk_state())
            if u.path == "/api/desk/key":
                DESK.capture = True
                if not DESK.on:
                    return self.send({"error": "Switch the desk mic on first, then set the key."}, 400)
                return self.send({"ok": True})
            if u.path == "/api/desk/savekey":
                st = settings(); st.setdefault("desk", {})["key"] = DESK.cfg.get("key"); E.save_json(DATA / "settings.json", st)
                return self.send({"ok": True})
            if u.path == "/api/link/save":
                st = settings()
                g = st.get("github", {})
                g.update({"owner": d.get("owner", "").strip(), "repo": (d.get("repo") or "crate-boy-data").strip()})
                if d.get("token"):
                    g["token"] = d["token"].strip()
                st["github"] = g
                E.save_json(DATA / "settings.json", st)
                link = make_link()
                if link is None:
                    return self.send({"error": "Fill in account, repo and token."}, 400)
                try:
                    info = link.test()
                except G.GitHubError as ex:
                    return self.send({"error": str(ex)}, 400)
                if not info.get("private"):
                    return self.send({"error": "That repo is PUBLIC. Use the private crate-boy-data."}, 400)
                LINK["now"] = True
                return self.send({"ok": True})
            if u.path == "/api/link/sync":
                LINK["now"] = True
                return self.send({"ok": True})
            if u.path == "/api/desk/gain":
                g = max(0.0, min(40.0, float(d.get("gain_db", 0))))
                DESK.cfg["gain_db"] = g
                st = settings(); st.setdefault("desk", {})["gain_db"] = g; E.save_json(DATA / "settings.json", st)
                return self.send({"ok": True, "gain_db": g})
            if u.path == "/api/inbox/delete":
                ids = set(d.get("ids", []))
                with SESS_LOCK:
                    items = inbox()
                    for i in items:
                        if i["id"] in ids and i.get("wav"):
                            (OUT / "desk" / safe(i["wav"])).unlink(missing_ok=True)
                    E.save_json(INBOX, [i for i in items if i["id"] not in ids])
                return self.send({"ok": True})
            if u.path == "/api/inbox/edit":
                with SESS_LOCK:
                    items = inbox()
                    for i in items:
                        if i["id"] == d["id"]:
                            i["text"] = d.get("text", i["text"]).strip()
                    E.save_json(INBOX, items)
                return self.send({"ok": True})
            if u.path == "/api/inbox/session":
                ids = d.get("ids", [])
                with SESS_LOCK:
                    items = inbox()
                    pick = [i for i in items if i["id"] in set(ids)]
                    if not pick:
                        return self.send({"error": "Select some notes first."}, 400)
                    pick.sort(key=lambda i: (i["day"], i["t"]))
                    notes = [[f"{i['day'][8:10]}.{i['day'][5:7]} {i['t']}", i["text"]] for i in pick]
                    cards = []
                    for t, txt in notes:
                        cards += cards_from_text(txt, t)
                    days = sorted({i["day"] for i in pick})
                    name = d.get("name") or ("Desk " + (days[0][8:10] + "." + days[0][5:7] if len(days) == 1
                                                        else days[0][8:10] + "." + days[0][5:7] + "-" + days[-1][8:10] + "." + days[-1][5:7]))
                    sess_ = make_session(name, cards, "desk", notes)
                    for i in items:
                        if i["id"] in set(ids):
                            i["used"] = True
                    E.save_json(INBOX, items)
                return self.send(sess_)
            if u.path == "/api/desk/install":
                threading.Thread(target=desk_install, daemon=True).start()
                return self.send({"ok": True})
            if u.path == "/api/quit":
                threading.Timer(0.3, lambda: os._exit(0)).start()
                return self.send({"ok": True})
            if u.path == "/api/library/reload":
                library(reload=True)
                return self.send({"ok": STATE["lib"] is not None, "error": STATE["lib_error"]})
            if u.path == "/api/transcribe":
                if STATE["job"].get("state") in ("loading", "running"):
                    return self.send({"error": "Already transcribing something."}, 409)
                st = settings()
                st.update({k: d[k] for k in ("model", "language") if k in d})
                E.save_json(DATA / "settings.json", st)
                threading.Thread(target=transcribe_job, args=(d["file"], st["model"], st["language"]),
                                 daemon=True).start()
                return self.send({"ok": True})
            if u.path == "/api/settings":
                st = settings(); st.update(d); E.save_json(DATA / "settings.json", st)
                return self.send(st)
            if u.path == "/api/session/from_text":
                cards = cards_from_text(d.get("text", ""))
                if not cards:
                    return self.send({"error": "No notes found in that text."}, 400)
                s = make_session(d.get("name") or "Typed notes " + time.strftime("%d.%m %H:%M"), cards, "text")
                return self.send(s)
            if u.path == "/api/session/from_file":
                p = IN / safe(d["file"])
                txt = p.read_text(encoding="utf-8", errors="replace")
                cards, notes = [], []
                for line in txt.splitlines():
                    if not line.strip():
                        continue
                    m = re.match(r"\s*\[(.*?)\]\s*(.*)", line)
                    t, body = (m.group(1), m.group(2)) if m else ("", line.strip())
                    notes.append([t, body])
                    cards += cards_from_text(body, t)
                s = make_session(p.stem, cards, "text", notes)
                return self.send(s)
            if u.path == "/api/session/from_tracks":
                lib = library()
                cards = []
                for tid in d.get("trackids", [])[:300]:
                    t = lib.by_id.get(tid)
                    if t is None:
                        continue
                    info = lib.info(t)
                    com = info["comment"] if E.is_tag_like(info["comment"]) else ""
                    cards.append({"id": uuid.uuid4().hex[:6], "t": "", "text": "(from your crate)", "status": "new",
                                  "exported": False, "source": "crate", "title": info["name"], "artist": info["artist"],
                                  "rating": info["rating"] or None, "tags": E.parse_tags(com)[0], "genre_spec": "",
                                  "match": dict(info, score=1.0), "cands": []})
                if not cards:
                    return self.send({"error": "No tracks selected."}, 400)
                s = make_session(d.get("name") or "Crate pick " + time.strftime("%d.%m %H:%M"), cards, "crate")
                return self.send(s)
            if u.path == "/api/session/save":
                s = d["session"]
                with SESS_LOCK:
                    srv = get_session(s["id"]) if s["id"] != "pending" else None
                    if srv:
                        ids = {c["id"] for c in s["cards"]}
                        s["cards"] += [c for c in srv["cards"] if c["id"] not in ids and not s.get("_rebuilt")]
                        s["notes"] = srv.get("notes", s.get("notes"))
                    for c in s["cards"]:
                        c["comment"] = E.format_tags(c["tags"])
                    save_session(s)
                return self.send({"ok": True, "n": len(s["cards"])})
            if u.path == "/api/session/delete":
                if d["id"] == "pending":
                    E.save_json(DATA / "pending.json", [])
                else:
                    (SESS / f"{safe(d['id'])}.json").unlink(missing_ok=True)
                return self.send({"ok": True})
            if u.path == "/api/session/claude":
                s = get_session(d["id"])
                tracks = E.parse_claude_tracks(d.get("text", ""))
                if tracks is not None and s["id"] != "pending":
                    n = rebuild_from_claude(s, tracks)
                    save_session(s)
                    return self.send({"ok": True, "updated": n, "rebuilt": True, "session": get_session(d["id"])})
                got = E.parse_claude_csv(d.get("text", ""))
                n = 0
                for c in s["cards"]:
                    g = got.get(c["id"])
                    if not g:
                        continue
                    n += 1
                    retitle = (g["title"] and E.norm(g["title"]) != E.norm(c.get("title"))) or \
                              (g["artist"] and E.norm(g["artist"]) != E.norm(c.get("artist")))
                    c.update({k: v for k, v in g.items() if v not in ("", None) or k == "genre_spec"})
                    c["source"] = "claude"
                    if retitle:
                        c["match"] = None
                        rematch(c, force=True)
                save_session(s)
                return self.send({"ok": True, "updated": n, "session": get_session(d["id"])})
            if u.path == "/api/rematch":
                c = d["card"]
                rematch(c, force=True)
                return self.send(c)
            if u.path == "/api/export":
                return self.send(export(d.get("fixes", [])))
            if u.path == "/api/open":
                target = {"out": OUT, "in": IN}.get(d.get("what"), OUT)
                if os.name == "nt":
                    os.startfile(str(target))
                return self.send({"ok": True})
            self.send({"error": "not found"}, 404)
        except Exception as ex:
            log("POST error", traceback.format_exc())
            self.send({"error": str(ex)}, 500)


def open_window(url):
    if os.environ.get("CRATEBOY_NO_BROWSER"):
        return
    edge = shutil.which("msedge")
    for c in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"):
        if not edge and os.path.exists(c):
            edge = c
    if edge:
        try:
            subprocess.Popen([edge, f"--app={url}", "--window-size=1180,920",
                              f"--user-data-dir={DATA / 'window'}", "--no-first-run"])
            return
        except OSError:
            pass
    webbrowser.open(url)


def watchdog():
    while True:
        time.sleep(15)
        busy = STATE["job"].get("state") in ("loading", "running") or DESK.on or DESK_STATE["busy"] or LINK["busy"] or PHONE_Q
        if not busy and time.time() - STATE["ping"] > 90:
            log("window closed, quitting")
            os._exit(0)


def main():
    url = f"http://127.0.0.1:{PORT}/"
    srv = None
    for attempt in range(2):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
            break
        except OSError:
            # Something is already running. Same version: just show its window.
            # Older version (after an update): ask it to quit, then take over.
            import urllib.request
            try:
                v = json.load(urllib.request.urlopen(url + "api/version", timeout=3)).get("version")
            except Exception:
                v = None
            if v == VERSION or attempt == 1:
                open_window(url)
                return
            try:
                urllib.request.urlopen(urllib.request.Request(url + "api/quit", data=b"{}"), timeout=3)
            except Exception:
                pass
            time.sleep(1.5)
    if srv is None:
        return
    log("Crate Boy started", url)
    threading.Thread(target=library, daemon=True).start()
    threading.Thread(target=watchdog, daemon=True).start()
    threading.Thread(target=phone_worker, daemon=True).start()
    threading.Thread(target=link_loop, daemon=True).start()
    if settings().get("desk_auto") and D.parts_available()[0]:
        try:
            desk_start({})
        except Exception as ex:
            DESK.err = str(ex)
            log("desk autostart failed", ex)
    open_window(url)
    srv.serve_forever()


if __name__ == "__main__":
    main()
