"""
Crate Boy engine: KP's tag legend, tag parsing/formatting, the offline draft translator,
library matching, library health check and Rekordbox XML export.
Everything here is local. No network.
"""
import csv
import datetime as dt
import difflib
import io
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

# ---------------------------------------------------------------- legend
ENERGY = "ALMHP"                 # written in arc order, not sorted
VIBE = "WDIFUSCGQ@TY"            # always in this order
ELEM = "$XV"                     # $ (sample) is grouped with the elements
ROLE = "OBRE"
ALL = set(ENERGY + VIBE + ELEM + ROLE)
GROUP_OF = {**{c: "energy" for c in ENERGY}, **{c: "vibe" for c in VIBE},
            **{c: "elem" for c in ELEM}, **{c: "role" for c in ROLE}}

LEGEND = {}          # filled from the private legend.json (crate-boy-data), never stored in this public code
SHORT = {}
RULES = []
VOCAB = ""
GENRES = []
PRIVATE = {"loaded": False, "source": ""}

RATING_TO_XML = {0: 0, 1: 51, 2: 102, 3: 153, 4: 204, 5: 255}


def xml_to_stars(v):
    try:
        v = int(v or 0)
    except ValueError:
        return 0
    return min(5, round(v / 51))


def empty_tags():
    # ejoin: "-" = energy arc (from ... to ...), "&" = has both / jumps (not in between), e.g. L&P
    return {"energy": [], "ejoin": "-", "vibe": {}, "elem": {}, "role": {}}


# ---------------------------------------------------------------- tag string <-> structure
TOKEN = re.compile(r"([A-Z$@])((?:!|\?|\+(?![A-Z$@]))*)")


def is_tag_like(s):
    s = (s or "").strip()
    return bool(s) and bool(re.fullmatch(r"[A-Z$@!+?&\-/ ]+", s)) and bool(re.search(r"[A-Z$@]", s))


def parse_tags(s):
    """Comment string -> (tags, problems). Accepts sloppy input (typos, wrong order)."""
    tags, problems = empty_tags(), []
    s = (s or "").strip()
    for m in TOKEN.finditer(s):
        ch, mods = m.group(1), m.group(2)
        g = GROUP_OF.get(ch)
        if g is None:
            problems.append(f"unknown letter {ch}")
            continue
        if g == "energy":
            if [ch, mods] not in tags["energy"]:
                tags["energy"].append([ch, mods])
        else:
            tags[g][ch] = mods
    first = s.split("/")[0]
    if (re.search(r"\+(?=[ALMHP])", first) or "&" in first) and len(tags["energy"]) > 1:
        tags["ejoin"] = "&"                       # L&P = both at once (old "L+P" is read the same way)
        tags["energy"].sort(key=lambda x: ENERGY.index(x[0]))
    rest = TOKEN.sub("", s)
    if re.search(r"[^\-/ +!?&]", rest):
        problems.append("stray characters")
    return tags, problems


def format_tags(tags):
    parts = []
    if tags["energy"]:
        j = "&" if tags.get("ejoin") in ("+", "&") else "-"
        en = sorted(tags["energy"], key=lambda x: ENERGY.index(x[0])) if j == "&" else tags["energy"]
        parts.append(j.join(c + m for c, m in en))
    for g, order in (("vibe", VIBE), ("elem", ELEM), ("role", ROLE)):
        d = tags[g]
        if d:
            parts.append("-".join(c + d[c] for c in order if c in d))
    return " / ".join(parts)


def tags_empty(tags):
    return not (tags["energy"] or tags["vibe"] or tags["elem"] or tags["role"])


# ---------------------------------------------------------------- offline draft translator
NUM = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
       "0": 0, "1": 1, "2": 2, "3": 3, "4": 4, "5": 5,
       "null": 0, "eins": 1, "ein": 1, "einen": 1, "zwei": 2, "drei": 3, "vier": 4, "fünf": 5, "fuenf": 5}
NUMW = r"(zero|one|two|three|four|five|null|eins|einen|ein|zwei|drei|vier|fünf|fuenf|[0-5])"
STAR_PATTERNS = [
    re.compile(rf"\b{NUMW}[\s\-]*(?:stars?|sterne?n?|\*)", re.I),                     # three stars, 3-star, drei Sterne
    re.compile(rf"\b{NUMW}\s*(?:out of|of|von|aus)\s*(?:five|fünf|5)\b", re.I),       # three out of five
    re.compile(rf"\b(?:stars?|sterne?|rating|rated|rate it|rate|bewertung|give it|gebe? ihm|it's a|its a|that's a|thats a)"
               rf"\s*[:,.]?\s*(?:(?:a|an|it|eine?|ihm)\s+){{0,2}}{NUMW}\b", re.I),       # rating three, rated a four
]

# phrase -> letter, from the private legend.json. Longer phrases are matched first; "" = consume, give nothing.
SYN = {}
SYN_RE = None


def _build_syn_re():
    global SYN_RE
    keys = sorted(SYN, key=len, reverse=True)
    SYN_RE = re.compile(r"\b(" + "|".join(re.escape(k) for k in keys) + r")\b", re.I) if keys else None


def load_private(path):
    """Load KP's legend, word list, translation rules and vocabulary from his private data file."""
    import json as _json
    try:
        d = _json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    LEGEND.clear(); LEGEND.update(d.get("legend", {}))
    SHORT.clear(); SHORT.update(d.get("short", {}))
    SYN.clear(); SYN.update(d.get("syn", {}))
    RULES[:] = d.get("prompt_rules", [])
    GENRES[:] = d.get("genre_clusters", [])
    globals()["VOCAB"] = d.get("vocab", "")
    _build_syn_re()
    PRIVATE.update(loaded=True, source=str(path))
    return True
INTENS = {"very", "really", "super", "so", "extremely", "mega", "hella", "strong", "strongly",
          "heavily", "totally", "massively", "insanely", "crazy", "proper", "full", "fully"}
NEGS = {"not", "no", "isn't", "isnt", "without", "never", "zero", "non"}
RISING = re.compile(r"\b(rising|rises|builds?|building|goes up|going up|climbing|climbs|increasing|ramps? up)\b", re.I)
FALLING = re.compile(r"\b(falling|falls|goes down|going down|dropping|decreasing|winds? down|cools? down|cooling)\b", re.I)


def split_notes(text):
    """Free text -> list of note strings, split at the word 'next'."""
    text = (text or "").replace("\r", "")
    chunks = re.split(r"(?i)(?:^|\s|[.,!?])(?=next\b)", text)
    return [c.strip() for c in chunks if c and c.strip(" .,\n")]


def draft_from_note(text):
    """Speech -> draft card fields (title, artist, rating, tags, genre_spec)."""
    t = " " + (text or "").strip() + " "
    title = artist = ""
    rating = None
    genre_spec = ""

    # title: after "next" up to the rating or the first sentence end
    m = re.search(r"(?i)\b(?:next|track|title)\b[\s:,.\-]*(.*)", t)
    head = m.group(1) if m else ""
    cut = len(head)
    for p in STAR_PATTERNS + [re.compile(r"[.;!?](\s|$)")]:
        mm = p.search(head)
        if mm:
            cut = min(cut, mm.start())
    title = head[:cut].strip(" ,.:-")
    if m:
        t = t.replace(m.group(0)[:len(m.group(0)) - len(head) + cut], " ", 1)
    if " by " in title.lower():
        i = title.lower().rfind(" by ")
        title, artist = title[:i].strip(), title[i + 4:].strip()

    # rating
    for p in STAR_PATTERNS:
        mm = p.search(t)
        if mm:
            rating = NUM.get(mm.group(1).lower())
            t = t[:mm.start()] + " " + t[mm.end():]
            break
    if rating is None and re.search(r"(?i)\b(trash|delete it|deletable)\b", t):
        rating = 1

    # genre: "genre ..." until sentence end
    mm = re.search(r"(?i)\bgenres?\b[\s:,]*([^.;!?]*)", t)
    if mm:
        genre_spec = genre_spec_from_speech(mm.group(1))
        t = t[:mm.start()] + " " + t[mm.end():]

    # letters
    tags = empty_tags()
    for mm in (SYN_RE.finditer(t) if SYN_RE else []):
        letter = SYN[mm.group(1).lower()]
        if not letter:
            continue
        clause = re.split(r"[,.;!?]", t[:mm.start()].lower())[-1]   # only words in the same clause count
        before = clause.split()[-2:]
        if any(w in NEGS for w in before):
            continue
        strong = any(w in INTENS for w in before)
        g = GROUP_OF[letter]
        if g == "energy":
            mod = ""
            if letter == "P" and (strong or re.match(r"\s*(plus|\+)", t[mm.end():], re.I)):
                mod = "+"
            if not any(c == letter for c, _ in tags["energy"]):
                tags["energy"].append([letter, mod])
        else:
            cur = tags[g].get(letter)
            tags[g][letter] = "!" if (strong or cur == "!") else ""
    if re.search(r"(?i)\b(at the same time|not in between|both|mix between|combination)\b", t) and len(tags["energy"]) > 1:
        tags["ejoin"] = "&"
        tags["energy"].sort(key=lambda x: ENERGY.index(x[0]))
    elif RISING.search(t):
        tags["energy"].sort(key=lambda x: ENERGY.index(x[0]))
    elif FALLING.search(t):
        tags["energy"].sort(key=lambda x: -ENERGY.index(x[0]))
    return {"title": title, "artist": artist, "rating": rating, "tags": tags, "genre_spec": genre_spec}


HALLUCINATIONS = ("subtitles by the amara", "thank you for watching", "thanks for watching",
                  "please subscribe", "transcription by", "subtitles by", "untertitel")


def is_hallucination(t):
    """Phrases Whisper invents on silence or music."""
    low = (t or "").strip().lower()
    return not low or any(h in low for h in HALLUCINATIONS)


def genre_spec_from_speech(s):
    items = [i.strip(" ,.") for i in re.split(r",|\band\b|\bplus\b(?!\s*$)", s, flags=re.I) if i.strip(" ,.")]
    out, changes = [], False
    for it in items:
        low = it.lower()
        mm = re.match(r"(not|no|minus|remove|without|drop)\s+(.*)", low)
        if mm:
            out.append("-" + mm.group(2).strip().title()); changes = True
            continue
        mm = re.match(r"(add|also|plus|more)\s+(.*)", low)
        if mm:
            out.append("+" + mm.group(2).strip().title()); changes = True
            continue
        out.append(it.strip().title())
    if changes:
        return "; ".join(x if x[0] in "+-" else "+" + x for x in out)
    return ", ".join(out)


def apply_genre(current, spec):
    spec = (spec or "").strip()
    if not spec:
        return current or ""
    if spec[0] not in "+-":
        return spec
    genres = [g.strip() for g in (current or "").split(",") if g.strip()]
    for op in [o.strip() for o in spec.split(";") if o.strip()]:
        name = op[1:].strip()
        if op[0] == "-":
            genres = [g for g in genres if norm(g) != norm(name) and norm(name) not in norm(g).split()]
        elif all(norm(g) != norm(name) for g in genres):
            genres.append(name)
    return ", ".join(genres)


# ---------------------------------------------------------------- library
NOISE = re.compile(r"\b(original mix|original|extended mix|remaster(ed)?|feat\.?|ft\.?)\b")


def norm(s):
    s = (s or "").lower()
    s = NOISE.sub(" ", s)
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


class Library:
    def __init__(self, path):
        self.path = Path(path)
        self.mtime = self.path.stat().st_mtime
        self.tree = ET.parse(self.path)
        self.tracks = self.tree.getroot().find("COLLECTION").findall("TRACK")
        self.by_id = {t.get("TrackID"): t for t in self.tracks}
        self._norm = [(norm(t.get("Name")), norm(t.get("Artist")), t) for t in self.tracks]

    def info(self, t):
        return {"trackid": t.get("TrackID"), "name": t.get("Name", ""), "artist": t.get("Artist", ""),
                "album": t.get("Album", ""), "genre": t.get("Genre", ""), "comment": t.get("Comments", ""),
                "rating": xml_to_stars(t.get("Rating")), "bpm": t.get("AverageBpm", ""),
                "key": t.get("Tonality", "")}

    def search(self, title, artist="", n=5):
        q, qa = norm(title), norm(artist)
        if not q and not qa:
            return []
        qw = set(q.split())
        scored = []
        for nt, na, t in self._norm:
            if qw and not (qw & set(nt.split())) and q not in nt and difflib.SequenceMatcher(None, q, nt).quick_ratio() < 0.6:
                continue
            s = 1.0 if q == nt else difflib.SequenceMatcher(None, q, nt).ratio()
            if q and nt and (q in nt or nt in q) and min(len(q), len(nt)) >= 3:
                s = max(s, 0.9)
            if qa:
                a = 1.0 if qa == na else 0.8 * difflib.SequenceMatcher(None, qa, na).ratio()
                s = 0.7 * s + 0.3 * a
            if s >= 0.5:
                scored.append((round(s, 3), t))
        scored.sort(key=lambda x: -x[0])
        return [dict(self.info(t), score=s) for s, t in scored[:n]]

    @staticmethod
    def sure(cands):
        """The single clear winner, or None."""
        if not cands or cands[0]["score"] < 0.8:
            return None
        if len(cands) > 1 and cands[1]["score"] >= 0.8 and cands[0]["score"] - cands[1]["score"] < 0.08:
            return None
        return cands[0]

    def health(self):
        st = {"total": len(self.tracks), "rated": 0, "tagged": 0, "complete": 0, "fix": 0,
              "junk": 0, "empty": 0, "stars": [0] * 6}
        issues = []
        for t in self.tracks:
            r = xml_to_stars(t.get("Rating"))
            st["stars"][r] += 1
            if r:
                st["rated"] += 1
            c = (t.get("Comments") or "").strip()
            if not c:
                st["empty"] += 1
                continue
            if not is_tag_like(c):
                st["junk"] += 1
                issues.append({"kind": "junk", **self.info(t), "fixed": ""})
                continue
            tags, probs = parse_tags(c)
            st["tagged"] += 1
            if tags["energy"] and tags["vibe"]:
                st["complete"] += 1
            canon = format_tags(tags)
            if probs or canon != c:
                st["fix"] += 1
                issues.append({"kind": "format", **self.info(t), "fixed": canon, "problems": probs})
            elif not (tags["energy"] and tags["vibe"]):
                issues.append({"kind": "incomplete", **self.info(t), "fixed": canon})
        return st, issues


def find_library(in_dir):
    """Newest Rekordbox collection XML in IN."""
    best = None
    for p in Path(in_dir).glob("*.xml"):
        try:
            with open(p, "rb") as f:
                head = f.read(2000)
        except OSError:
            continue
        if b"DJ_PLAYLISTS" in head and (best is None or p.stat().st_mtime > best.stat().st_mtime):
            best = p
    return best


def build_import_xml(lib, updates, out_path, playlist_name):
    """updates: list of {trackid, rating|None, comment|None, genre|None}"""
    root = ET.Element("DJ_PLAYLISTS", Version="1.0.0")
    ET.SubElement(root, "PRODUCT", Name="rekordbox", Version="6.0.0", Company="AlphaTheta")
    coll = ET.SubElement(root, "COLLECTION", Entries="0")
    pls = ET.SubElement(root, "PLAYLISTS")
    rootnode = ET.SubElement(pls, "NODE", Type="0", Name="ROOT", Count="1")
    pl = ET.SubElement(rootnode, "NODE", Name=playlist_name, Type="1", KeyType="0", Entries="0")
    seen, n = set(), 0
    for u in updates:
        t = lib.by_id.get(u["trackid"])
        if t is None or u["trackid"] in seen:
            continue
        seen.add(u["trackid"])
        new = ET.fromstring(ET.tostring(t))        # full copy keeps cues, grid, key, everything
        if u.get("rating") is not None:
            new.set("Rating", str(RATING_TO_XML[max(0, min(5, int(u["rating"])))]))
        if u.get("comment") is not None:
            new.set("Comments", u["comment"])
        if u.get("genre") is not None:
            new.set("Genre", u["genre"])
        coll.append(new)
        ET.SubElement(pl, "TRACK", Key=u["trackid"])
        n += 1
    coll.set("Entries", str(n))
    pl.set("Entries", str(n))
    ET.indent(root)
    ET.ElementTree(root).write(out_path, encoding="UTF-8", xml_declaration=True)
    return n


# ---------------------------------------------------------------- Claude round trip
def _legend_line():
    return "  ".join(f"{k}={v}" for k, v in LEGEND.items()) or "(legend not loaded - connect the phone link / GitHub in Crate Boy)"


def claude_prompt(cards):
    lines = [
        "Crate Boy notes - translate into my Rekordbox tags.",
        f"Legend: {_legend_line()}",
        "Format: ENERGY / VIBES / ELEMENTS / ROLE, letters joined by '-'. Vibes in order W-D-I-F-U-S-C-G-Q-@-T-Y. "
        "Elements $-X-V. Roles O-B-R-E. Leave out empty segments.",
        *RULES,
        "Return ONLY a CSV code block with header: id,title,artist,rating,comment,genre",
        "",
    ]
    for c in cards:
        lines.append(f"{c['id']} | {c.get('t', '')} | {c['text']}")
    return "\n".join(lines)


def claude_prompt_full(notes):
    """Whole transcript -> one prompt. Claude finds the tracks, merges add-ons and corrections."""
    lines = [
        "CRATE BOY - full transcript of a listening/DJ session. Turn it into one row per track for my Rekordbox tags.",
        f"Legend: {_legend_line()}",
        "Tag format: ENERGY / VIBES / ELEMENTS / ROLE, letters joined by '-'. Vibes in order W-D-I-F-U-S-C-G-Q-@-T-Y. "
        "Elements $-X-V. Roles O-B-R-E. Leave out empty segments.",
        *RULES,
        "Return ONLY a CSV code block with header: title,artist,rating,comment,genre,times,note",
        "times = the timestamps of every line you used for that track, space-separated. note = short, only if something is unclear.",
        "",
        "TRANSCRIPT:",
    ]
    for t, txt in notes:
        lines.append(f"[{t}] {txt}")
    return "\n".join(lines)


def parse_claude_tracks(text):
    """Claude's per-track CSV (rebuild mode) -> list of card fields, or None if it isn't that format."""
    text = re.sub(r"```[a-zA-Z]*", "", text or "")
    m = re.search(r"(?im)^\s*title\s*,\s*artist\s*,", text)
    if not m:
        return None
    rows = list(csv.DictReader(io.StringIO(text[m.start():].strip())))
    out = []
    for r in rows:
        r = {(k or "").strip().lower(): (v or "").strip() for k, v in r.items() if k}
        if not (r.get("title") or r.get("comment")):
            continue
        rating = r.get("rating", "")
        out.append({
            "title": r.get("title", ""), "artist": r.get("artist", ""),
            "rating": int(float(rating)) if re.fullmatch(r"\d+(\.\d+)?", rating) else None,
            "tags": parse_tags(r.get("comment", ""))[0], "genre_spec": r.get("genre", ""),
            "times": r.get("times", "").split(), "note": r.get("note", ""),
        })
    return out


def parse_claude_csv(text):
    text = re.sub(r"```[a-zA-Z]*", "", text or "")
    start = text.lower().find("id,")
    if start < 0:
        raise ValueError("No CSV header 'id,title,...' found in the pasted text.")
    rows = list(csv.DictReader(io.StringIO(text[start:].strip())))
    out = {}
    for r in rows:
        r = {(k or "").strip().lower(): (v or "").strip() for k, v in r.items()}
        if not r.get("id"):
            continue
        rating = r.get("rating", "")
        out[r["id"]] = {
            "title": r.get("title", ""), "artist": r.get("artist", ""),
            "rating": int(float(rating)) if re.fullmatch(r"\d+(\.\d+)?", rating) else None,
            "tags": parse_tags(r.get("comment", ""))[0], "genre_spec": r.get("genre", ""),
        }
    return out


# ---------------------------------------------------------------- small persistence helpers
def load_json(p, default):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def save_json(p, data):
    p = Path(p)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)


def today():
    return dt.date.today().isoformat()
