#!/usr/bin/env python3
"""
Shir (שיר) — live Hebrew + English lyrics for whatever Spotify is playing on your Mac.

  * Spotify desktop app  -> current track + playback position (AppleScript, no login)
  * LRCLIB               -> time-synced lyrics (free, no key)
  * Jev (TypeSafe)       -> picks which lyrics search result is really your song,
                            and flags translations that look off       (optional)
  * DeepSeek             -> English translation + transliteration        (cached)

Run:   python3 shir.py            (opens in your browser)
       python3 shir.py --float    (always-on-top window; needs `pip3 install pywebview`)
       python3 shir.py --demo "שיר" "אמן"   (fake player, for trying it without Spotify)

Keys go in ~/.shir/config.json or env vars: DEEPSEEK_API_KEY, TYPESAFE_API_KEY.
Python 3.9+ standard library only.
"""
import argparse
import difflib
import json
import os
import re
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOME = Path.home() / ".shir"
CACHE = HOME / "cache"
PORT = 5799
VERSION = "1.9"
UA = "Shir/1.0 (personal lyrics viewer)"

# ----------------------------------------------------------------------------- config

def load_config():
    cfg = {
        "deepseek_api_key": "",
        "typesafe_api_key": "",
        # Any OpenAI-compatible endpoint works here (DeepSeek, OpenRouter, OpenAI, ...).
        "llm_base_url": "https://api.deepseek.com",
        "llm_model": "deepseek-chat",
        "jev_model": "jev-latest",
        "jev_openrouter_model": "typesafe/jev-1.14",
    }
    f = HOME / "config.json"
    if f.exists():
        try:
            cfg.update({k: v for k, v in json.loads(f.read_text()).items() if v})
        except Exception as e:
            print(f"! could not read {f}: {e}")
    env = {
        "DEEPSEEK_API_KEY": "deepseek_api_key",
        "TYPESAFE_API_KEY": "typesafe_api_key",
        "SHIR_LLM_BASE_URL": "llm_base_url",
        "SHIR_LLM_MODEL": "llm_model",
    }
    if cfg.get("llm_api_key"):  # friendlier name for any provider's key (OpenRouter, DeepSeek, ...)
        cfg["deepseek_api_key"] = cfg["llm_api_key"]
    for e, k in env.items():
        if os.environ.get(e):
            cfg[k] = os.environ[e]
    return cfg


CFG = load_config()

# ----------------------------------------------------------------------------- http helper

def http_json(url, payload=None, headers=None, timeout=30):
    data = json.dumps(payload).encode() if payload is not None else None
    h = {"User-Agent": UA, "Accept": "application/json"}
    if data is not None:
        h["Content-Type"] = "application/json"
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        body = e.read().decode(errors="replace")[:300]
        raise RuntimeError(f"HTTP {e.code} from {url.split('?')[0]}: {body}") from None

# ----------------------------------------------------------------------------- Spotify

SEP = "\x1f"
NOW_SCRIPT = '''
if application "Spotify" is not running then return "NOT_RUNNING"
tell application "Spotify"
  if player state is stopped then return "STOPPED"
  set t to current track
  set s to ASCII character 31
  return (id of t) & s & (name of t) & s & (artist of t) & s & (album of t) & s & ¬
    (duration of t as text) & s & (player position as text) & s & (player state as text) & s & (artwork url of t)
end tell
'''


def osa(script):
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=5)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "osascript failed")
    return r.stdout.strip()


def num(s):
    return float(str(s).replace(",", "."))  # some locales use a decimal comma


class SpotifyPlayer:
    def now(self):
        try:
            out = osa(NOW_SCRIPT)
        except Exception as e:
            return {"state": "error", "error": str(e)}
        if out in ("NOT_RUNNING", "STOPPED"):
            return {"state": out.lower()}
        p = out.split(SEP)
        if len(p) < 8:
            return {"state": "error", "error": "unexpected reply from Spotify"}
        return {
            "state": p[6],  # playing / paused
            "id": p[0], "title": p[1], "artist": p[2], "album": p[3],
            "duration": num(p[4]) / 1000.0, "position": num(p[5]), "artwork": p[7],
        }

    def control(self, action, value=None):
        cmds = {
            "seek": f"set player position to {float(value):.2f}",
            "playpause": "playpause", "next": "next track", "previous": "previous track",
        }
        if action in cmds:
            osa(f'tell application "Spotify" to {cmds[action]}')


class DemoPlayer:
    """Fake player so you can try the UI without Spotify."""

    def __init__(self, title, artist, album="", duration=0):
        self.t = {"id": f"demo:{title}:{artist}", "title": title, "artist": artist,
                  "album": album, "duration": float(duration or 240), "artwork": ""}
        self.start = time.time()
        self.paused_at = None

    def _pos(self):
        ref = self.paused_at or time.time()
        return (ref - self.start) % self.t["duration"]

    def now(self):
        return dict(self.t, position=self._pos(),
                    state="paused" if self.paused_at else "playing")

    def set_duration(self, d):
        if d:
            self.t["duration"] = float(d)

    def control(self, action, value=None):
        if action == "seek":
            ref = self.paused_at or time.time()
            self.start = ref - float(value)
        elif action == "playpause":
            if self.paused_at:
                self.start += time.time() - self.paused_at
                self.paused_at = None
            else:
                self.paused_at = time.time()


PLAYER = None

# ----------------------------------------------------------------------------- LRCLIB

LRCLIB = "https://lrclib.net/api"
HEB = re.compile(r"[֐-׿]")


def clean_title(t):
    t = re.sub(r"\s*[\(\[](feat\.?|ft\.?|with|prod\.?)[^\)\]]*[\)\]]", "", t, flags=re.I)
    t = re.sub(r"\s+-\s+(.*remaster.*|live.*|radio edit|acoustic.*|.*version)$", "", t, flags=re.I)
    return t.strip()


def first_artist(a):
    # split "A, B" and "A feat. B" but keep duo names like "Static & Ben El" together
    return re.split(r",| x | feat\.? | ft\.? ", a, flags=re.I)[0].strip()


def lrclib_search(**params):
    params = {k: v for k, v in params.items() if v}
    try:
        return http_json(f"{LRCLIB}/search?{urllib.parse.urlencode(params)}", timeout=15) or []
    except Exception as e:
        print("! lrclib search:", e)
        return []


def lrclib_get(track):
    q = {"track_name": track["title"], "artist_name": track["artist"],
         "album_name": track["album"], "duration": round(track["duration"])}
    try:
        return http_json(f"{LRCLIB}/get?{urllib.parse.urlencode(q)}", timeout=15)
    except Exception as e:
        print("! lrclib get:", e)
        return None


def gather_candidates(track, extra_queries=(), base=True):
    title, artist = clean_title(track["title"]), first_artist(track["artist"])
    found = {}

    def add(items):
        for c in items or []:
            if c and c.get("id") and (c.get("syncedLyrics") or c.get("plainLyrics") or c.get("instrumental")):
                found.setdefault(c["id"], c)

    if base:
        exact = lrclib_get(track)
        if exact:
            exact["_exact"] = True
            add([exact])
        add(lrclib_search(track_name=title, artist_name=artist))
        add(lrclib_search(q=f"{title} {artist}"))
        add(lrclib_search(track_name=title))
    # LRCLIB mixes scripts freely: Hebrew title + English artist is common ("מאמי זה נגמר" by "Omer Adam").
    for t, a in extra_queries:
        for ta in {(t, a), (t, artist)}:
            add(lrclib_search(track_name=ta[0], artist_name=ta[1]))
            add(lrclib_search(q=f"{ta[0]} {ta[1]}"))
        add(lrclib_search(track_name=t))
    # Last resort: the artist's catalog, so fuzzy title matching can catch spelling differences.
    if extra_queries:
        for a in {extra_queries[0][1], artist}:
            add(lrclib_search(q=a))
    return list(found.values())


def heuristic(track, c):
    sim = lambda a, b: difflib.SequenceMatcher(None, (a or "").lower(), (b or "").lower()).ratio()
    # compare against Spotify's names and any Hebrew spelling we guessed for them
    names = [(track["title"], track["artist"])] + list(track.get("aliases", []))
    s = max(2 * sim(clean_title(t), clean_title(c.get("trackName", ""))) +
            sim(first_artist(a), c.get("artistName", "")) for t, a in names)
    if c.get("duration"):
        dd = abs(c["duration"] - track["duration"])
        s += 1.5 if dd <= 2 else 0.8 if dd <= 6 else 0 if dd <= 15 else -1.5
    s += 0.4 if c.get("syncedLyrics") else 0
    s += 1.0 if c.get("_exact") else 0
    return s


def fmt_dur(s):
    s = int(s or 0)
    return f"{s // 60}:{s % 60:02d}"


def lyric_preview(c, n=3):
    text = c.get("plainLyrics") or re.sub(r"\[[^\]]*\]", "", c.get("syncedLyrics") or "")
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    return " / ".join(lines[:n])

# ----------------------------------------------------------------------------- fallback sources
# Used only when LRCLIB has nothing that matches. lyrics.ovh has plain (untimed) lyrics.
# Shironet blocks scripts, so it is only offered as a link the user opens in their own browser.

SHIRONET = "https://shironet.mako.co.il"


def lyricsovh(artist, title):
    try:
        r = http_json(f"https://api.lyrics.ovh/v1/{urllib.parse.quote(artist)}/{urllib.parse.quote(title)}", timeout=8)
    except Exception:
        return None
    text = ((r or {}).get("lyrics") or "").strip()
    if text.lower().startswith("paroles de"):
        text = text.split("\n", 1)[-1].strip()
    if not text:
        return None
    return {"id": f"ovh:{artist}:{title}", "trackName": title, "artistName": artist, "albumName": "",
            "duration": None, "plainLyrics": text, "_provider": "lyrics.ovh"}


def fallback_candidates(track):
    from concurrent.futures import ThreadPoolExecutor
    title, artist = clean_title(track["title"]), first_artist(track["artist"])
    pairs = [(title, artist)] + list(track.get("aliases", []))[:2]
    with ThreadPoolExecutor(max_workers=4) as ex:
        found = list(ex.map(lambda ta: lyricsovh(ta[1], ta[0]), pairs))
    out, seen = [], set()
    for c in found:
        if c and c["plainLyrics"] not in seen:
            seen.add(c["plainLyrics"])
            out.append(c)
    return out


def shironet_link(track):
    """A Shironet search for the user to open in their browser (Hebrew spelling if we have one)."""
    t, a = next(iter(track.get("aliases") or []), (clean_title(track["title"]), first_artist(track["artist"])))
    return f"{SHIRONET}/search?q={urllib.parse.quote_plus(f'{t} {a}')}"


# ----------------------------------------------------------------------------- Jev (TypeSafe)

def jev_on():
    """Jev runs on a TypeSafe key, or through OpenRouter's Decisions API with an OpenRouter key."""
    return bool(CFG["typesafe_api_key"] or ("openrouter.ai" in CFG["llm_base_url"] and CFG["deepseek_api_key"]))


def jev(state, questions):
    if CFG["typesafe_api_key"]:
        url, key, model = "https://api.typesafe.ai/v1/systemone", CFG["typesafe_api_key"], CFG["jev_model"]
    else:
        url, key, model = "https://openrouter.ai/api/alpha/decisions", CFG["deepseek_api_key"], CFG["jev_openrouter_model"]
    r = http_json(url, {"state": state, "model": model, "questions": questions},
                  headers={"Authorization": f"Bearer {key}", "X-Title": "Shir"}, timeout=30)
    return (r or {}).get("answers", {})


def jev_pick(track, cands):
    """v2: one yes/no per candidate ("same song AND same artist?"), so Jev can say "none of these"."""
    cands = cands[:20]
    entries = {f"c{i}": {"title": c.get("trackName"), "artist": c.get("artistName"), "album": c.get("albumName"),
                         "length": fmt_dur(c.get("duration")) if c.get("duration") else "unknown",
                         "opens_with": lyric_preview(c, 2)}
               for i, c in enumerate(cands)}
    state = {
        "now_playing_on_spotify": {
            "title": track["title"], "artist": track["artist"], "album": track["album"],
            "length": fmt_dur(track["duration"]),
            "likely_hebrew_spelling": [{"title": t, "artist": a} for t, a in track.get("aliases", [])],
        },
        "lyrics_entries": entries,
        "note": ("Spotify often writes Hebrew names in English letters ('Omer Adam' = 'עומר אדם', "
                 "'Tel Aviv' = 'תל אביב'). The lyrics database may use either script, add track numbers, "
                 "'feat.' credits, or YouTube suffixes like 'הערוץ הרשמי'. Those are still the same song."),
    }
    qs = {f"same_{k}": {"type": "noul",
                        "instructions": f"lyrics_entries.{k} is the same song as now_playing_on_spotify, by the same "
                                        f"artist. A different song that shares the title, or a cover by another "
                                        f"artist, is NOT the same."}
          for k in entries}
    ans = jev(state, qs)
    p = {k: ans.get(f"same_{k}", {}).get("noul", 0.0) for k in entries}
    if not p:
        return None, {}
    score = lambda k: p[k] * (1.15 if cands[int(k[1:])].get("syncedLyrics") else 1.0) \
                           * (1.05 if abs((cands[int(k[1:])].get("duration") or 0) - track["duration"]) <= 3 else 1.0)
    best = max(p, key=score)
    info = {"by": "jev", "probability": round(p[best], 3)}
    if p[best] < 0.5:
        return None, info
    return cands[int(best[1:])], info


def jev_check_translations(pairs):
    """pairs: list of (hebrew, english). Returns list of probabilities the translation is faithful."""
    out = []
    for start in range(0, len(pairs), 40):
        chunk = pairs[start:start + 40]
        state = {f"line_{i}": {"hebrew": h, "english": e} for i, (h, e) in enumerate(chunk)}
        qs = {f"q{i}": {"type": "noul",
                        "instructions": f"The english in line_{i} is a faithful translation of the "
                                        f"hebrew in line_{i} (song lyrics; poetic phrasing is fine, "
                                        f"wrong meaning is not)."}
              for i in range(len(chunk))}
        ans = jev(state, qs)
        out += [ans.get(f"q{i}", {}).get("noul", 1.0) for i in range(len(chunk))]
    return out

# ----------------------------------------------------------------------------- LLM (DeepSeek)

def llm_json(system, user, timeout=60):
    base = CFG["llm_base_url"].rstrip("/")
    url = base + ("/chat/completions" if base.endswith("/v1") or "deepseek" in base else "/v1/chat/completions")
    body = {
        "model": CFG["llm_model"],
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "response_format": {"type": "json_object"},
        "temperature": 1.0,
    }
    if "openrouter.ai" in base:
        body["provider"] = {"sort": "throughput"}  # default routing sometimes lands on a slow host (8s vs 60s+)
        body["reasoning"] = {"enabled": False}      # the model sometimes "thinks" for 80s; not needed here
    r = http_json(url, body, headers={"Authorization": f"Bearer {CFG['deepseek_api_key']}"}, timeout=timeout)
    txt = r["choices"][0]["message"]["content"]
    txt = re.sub(r"^```(json)?|```$", "", txt.strip()).strip()
    return json.loads(txt)


def llm_guess_hebrew(track):
    """Spotify shows 'Omer Adam - Mami Ze Nigmar'; LRCLIB has 'מאמי זה נגמר'. Returns [(title, artist), ...]."""
    for attempt in range(2):
        try:
            r = llm_json(
                "You know Israeli music. Given Spotify metadata, give the song's title and main artist "
                "as written in Hebrew. Hebrew spelling varies (e.g. ממי / מאמי), so give up to 3 likely "
                "spellings, most likely first. "
                'Reply as JSON: {"options": [{"title": "...", "artist": "..."}]}',
                json.dumps({k: track[k] for k in ("title", "artist", "album")}, ensure_ascii=False), timeout=40)
            opts = r.get("options") or ([r] if r.get("title") else [])
            out = [(o.get("title", ""), o.get("artist", "")) for o in opts if o.get("title")]
            if out:
                return out[:3]
        except Exception as e:
            print("! hebrew guess:", e)
    return []


TRANSLATE_SYS = """You translate Hebrew song lyrics into English for a listener who is learning Hebrew.
For each numbered line give:
  "en": a natural, faithful English translation (keep the feeling; translate slang and idioms by meaning)
  "tr": a transliteration in English letters as an Israeli would pronounce it (e.g. "ani ohev otach", use "ch" for ח/כ, "tz" for צ)
Lines already in English: copy them to "en" and leave "tr" empty.
Use the whole song as context, but return exactly one entry per numbered line, in order.
Reply as JSON: {"lines": [{"i": 0, "en": "...", "tr": "..."}, ...]}"""


def translate(lines, track):
    """Small chunks in parallel (each sees the whole song for context): ~2-3s instead of 10s+."""
    from concurrent.futures import ThreadPoolExecutor
    uniq = list(dict.fromkeys(l for l in lines if l.strip()))
    result = {}
    full = "\n".join(lines)

    def one(chunk):
        user = (f"Song: {track['title']} — {track['artist']}\n\nFull lyrics (context):\n{full}\n\n"
                f"Lines to translate:\n" + json.dumps({str(i): l for i, l in enumerate(chunk)}, ensure_ascii=False))
        try:
            data = llm_json(TRANSLATE_SYS, user, timeout=45)
        except Exception as e:
            print("! translate:", e)
            return {}
        out = {}
        items = data.get("lines", []) if isinstance(data, dict) else []
        for n, it in enumerate(items):
            if not isinstance(it, dict):
                continue
            try:
                i = int(it.get("i", n))
            except (TypeError, ValueError):
                i = n
            if 0 <= i < len(chunk) and (it.get("en") or "").strip():
                out[chunk[i]] = {"en": it["en"].strip(), "tr": (it.get("tr") or "").strip()}
        return out

    # Providers sometimes return an empty reply, so retry whatever is still missing (once more).
    for attempt in range(2):
        todo = [l for l in uniq if l not in result]
        if not todo:
            break
        chunks = [todo[i:i + 10] for i in range(0, len(todo), 10)]
        with ThreadPoolExecutor(max_workers=8) as ex:
            for part in ex.map(one, chunks):
                result.update(part)
    return result


# ----------------------------------------------------------------------------- lyrics pipeline

TS = re.compile(r"\[(\d+):(\d+(?:[.,]\d+)?)\]")


def parse_lyrics(c):
    if c.get("syncedLyrics"):
        out = []
        for raw in c["syncedLyrics"].splitlines():
            stamps = TS.findall(raw)
            text = TS.sub("", raw).strip()
            for m, s in stamps:
                out.append({"t": int(m) * 60 + num(s), "he": text})
        out.sort(key=lambda x: x["t"])
        return out, True
    lines = [l.strip() for l in (c.get("plainLyrics") or "").splitlines()]
    # collapse runs of blank lines
    out, prev_blank = [], True
    for l in lines:
        if not l and prev_blank:
            continue
        out.append({"t": None, "he": l})
        prev_blank = not l
    return out, False


JOBS = {}     # track id -> {"stage": str}
LOCK = threading.Lock()


def cache_path(tid):
    return CACHE / (re.sub(r"[^A-Za-z0-9_.-]", "_", tid) + ".json")


def build(track, force_cand=None, pasted=None):
    tid = track["id"]
    stage = lambda s: JOBS.__setitem__(tid, {"stage": s})
    try:
        stage("Looking for lyrics…")
        cands = gather_candidates(track) if not pasted else []
        good = lambda c: HEB.search(lyric_preview(c, 2)) and abs((c.get("duration") or 0) - track["duration"]) <= 4
        if not pasted and CFG["deepseek_api_key"] and not HEB.search(track["title"]) and not any(good(c) for c in cands):
            stage("Looking for the Hebrew title…")
            track = dict(track, aliases=llm_guess_hebrew(track))
            cands += [c for c in gather_candidates(track, track["aliases"], base=False)
                      if c["id"] not in {x["id"] for x in cands}]
        cands.sort(key=lambda c: heuristic(track, c), reverse=True)

        chosen, info = None, {}
        if pasted:
            chosen = {"id": "pasted", "trackName": track["title"], "artistName": track["artist"],
                      "plainLyrics": pasted, "_provider": "pasted by you"}
            info = {"by": "you"}
        elif force_cand:
            chosen = next((c for c in cands if c["id"] == force_cand), None) \
                or http_json(f"{LRCLIB}/get/{force_cand}")
            info = {"by": "you"}
        elif cands:
            top = cands[0]
            obvious = top.get("_exact") and top.get("syncedLyrics")
            if obvious:
                chosen, info = top, {"by": "exact match"}
            elif jev_on():
                stage("Jev is picking the right lyrics…")
                try:
                    chosen, info = jev_pick(track, cands)
                except Exception as e:
                    print("! jev pick:", e)
                    chosen, info = top, {"by": "best guess", "note": f"Jev error: {e}"}
            elif heuristic(track, top) >= 2.5:
                chosen, info = top, {"by": "best guess"}

        if not chosen and not pasted and not force_cand:
            # LRCLIB had nothing that fits: try Shironet and lyrics.ovh
            if CFG["deepseek_api_key"] and not HEB.search(track["title"]) and not track.get("aliases"):
                stage("Looking for the Hebrew title…")
                track = dict(track, aliases=llm_guess_hebrew(track))
            stage("Trying lyrics.ovh…")
            fb = fallback_candidates(track)
            fb.sort(key=lambda c: heuristic(track, c), reverse=True)
            fb_pick, fb_info = None, {}
            if fb and jev_on():
                stage("Jev is checking the results…")
                try:
                    fb_pick, fb_info = jev_pick(track, fb)
                except Exception as e:
                    print("! jev pick (fallback):", e)
            elif fb and heuristic(track, fb[0]) >= 2.5:
                fb_pick, fb_info = fb[0], {"by": "best guess"}
            if fb_pick and (fb_pick.get("plainLyrics") or "").strip():
                chosen, info = fb_pick, fb_info

        res = {
            "track": {k: track[k] for k in ("id", "title", "artist", "album", "duration")},
            "shironet": shironet_link(track),
            "match": info,
            "candidates": [{"id": c["id"], "title": c.get("trackName"), "artist": c.get("artistName"),
                            "album": c.get("albumName"), "duration": c.get("duration"),
                            "synced": bool(c.get("syncedLyrics")), "preview": lyric_preview(c, 2)}
                           for c in cands[:25]],
        }
        if not chosen:
            res.update(status="notfound", lines=[])
        elif chosen.get("instrumental"):
            res.update(status="instrumental", lines=[])
        else:
            lines, synced = parse_lyrics(chosen)
            res.update(status="ok", synced=synced, lines=lines, source_id=chosen["id"],
                       provider=chosen.get("_provider", "LRCLIB"),
                       source=f"{chosen.get('trackName')} — {chosen.get('artistName')}")
            texts = [l["he"] for l in lines]
            if any(HEB.search(t) for t in texts):
                if CFG["deepseek_api_key"]:
                    stage("Translating…")
                    tr = translate(texts, track)
                    for l in lines:
                        l.update(tr.get(l["he"], {}))
                    heb = {t for t in texts if HEB.search(t)}
                    if len(heb & set(tr)) < 0.8 * len(heb):
                        res["warning"] = "Translation came back incomplete — click refresh to try again."
                    if jev_on():
                        stage("Jev is double-checking the translation…")
                        try:
                            pairs = [(h, tr[h]["en"]) for h in dict.fromkeys(texts)
                                     if h in tr and tr[h]["en"] and HEB.search(h)]
                            ps = dict(zip([p[0] for p in pairs], jev_check_translations(pairs)))
                            for l in lines:
                                if l["he"] in ps:
                                    l["ok"] = round(ps[l["he"]], 2)
                        except Exception as e:
                            print("! jev check:", e)
                else:
                    res["warning"] = "Add a DeepSeek API key to get translations."
        res["built"] = time.time()
        CACHE.mkdir(parents=True, exist_ok=True)
        if res["status"] == "ok" and not (res.get("warning")):
            cache_path(tid).write_text(json.dumps(res, ensure_ascii=False))
        JOBS[tid] = {"stage": "done", "result": res}
    except Exception as e:
        traceback.print_exc()
        JOBS[tid] = {"stage": "done", "result": {"status": "error", "error": str(e), "lines": []}}


def lyrics_for(track, force_cand=None, refresh=False, pasted=None):
    tid = track["id"]
    p = cache_path(tid)
    if not refresh and not force_cand and not pasted and p.exists():
        return json.loads(p.read_text())
    with LOCK:
        job = JOBS.get(tid)
        if force_cand or refresh or pasted:
            if job and job["stage"] != "done":
                return {"status": "working", "stage": job["stage"]}
            job = None
            p.unlink(missing_ok=True)
        if job is None:
            JOBS[tid] = {"stage": "Starting…"}
            threading.Thread(target=build, args=(track, force_cand, pasted), daemon=True).start()
            return {"status": "working", "stage": "Starting…"}
    if job["stage"] == "done":
        res = job["result"]
        if res.get("status") != "ok" or res.get("warning"):
            # don't hold failures forever: allow a retry after a minute
            if time.time() - res.get("built", 0) > 60:
                JOBS.pop(tid, None)
        return res
    return {"status": "working", "stage": job["stage"]}

# ----------------------------------------------------------------------------- web server

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json"):
        b = body if isinstance(body, bytes) else (
            json.dumps(body, ensure_ascii=False).encode() if ctype == "application/json" else body.encode())
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = dict(urllib.parse.parse_qsl(u.query))
        try:
            if u.path == "/":
                return self.send(200, PAGE, "text/html")
            if u.path == "/api/now":
                return self.send(200, PLAYER.now())
            if u.path == "/api/lyrics":
                now = PLAYER.now()
                if not now.get("id"):
                    return self.send(200, {"status": "idle"})
                res = lyrics_for(now, force_cand=int(q["pick"]) if q.get("pick") else None,
                                 refresh=q.get("refresh") == "1")
                return self.send(200, res)
            if u.path == "/api/status":
                return self.send(200, {"translation": bool(CFG["deepseek_api_key"]),
                                       "jev": jev_on(),
                                       "model": CFG["llm_model"]})
            self.send(404, {"error": "not found"})
        except Exception as e:
            traceback.print_exc()
            self.send(500, {"error": str(e)})

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            if u.path == "/api/control":
                PLAYER.control(body.get("action"), body.get("value"))
                return self.send(200, {"ok": True})
            if u.path == "/api/paste":
                now = PLAYER.now()
                text = (body.get("text") or "").strip()
                if not now.get("id") or not text:
                    return self.send(400, {"error": "nothing playing or no text"})
                return self.send(200, lyrics_for(now, pasted=text[:20000]))
            self.send(404, {"error": "not found"})
        except Exception as e:
            self.send(500, {"error": str(e)})


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Shir</title>
<style>
:root{--bg:#0e0d12;--fg:#f4f1ea;--dim:rgba(244,241,234,.38);--faint:rgba(244,241,234,.16);
  --accent:#f2c14e;--en:#cfe3ff;--tr:#e9d7ff;--size:1}
*{box-sizing:border-box}
html,body{margin:0;height:100%;background:var(--bg);color:var(--fg);
  font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text","Helvetica Neue","Arial Hebrew",sans-serif;
  -webkit-font-smoothing:antialiased;overflow:hidden}
#bg{position:fixed;inset:-60px;background-size:cover;background-position:center;
  filter:blur(60px) saturate(1.4) brightness(.45);transform:scale(1.15);transition:background-image 1s;z-index:0}
#bg::after{content:"";position:absolute;inset:0;background:linear-gradient(180deg,rgba(14,13,18,.35),rgba(14,13,18,.85))}
header{position:fixed;top:0;left:0;right:0;z-index:3;display:flex;gap:12px;align-items:center;
  padding:14px 18px;background:linear-gradient(180deg,rgba(10,9,14,.85),rgba(10,9,14,0));-webkit-app-region:drag}
#art{width:46px;height:46px;border-radius:7px;object-fit:cover;background:var(--faint);flex:none;
  box-shadow:0 4px 18px rgba(0,0,0,.4)}
#meta{min-width:0;flex:1}
#title{font-weight:700;font-size:15px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#artist{font-size:13px;color:var(--dim);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ctl{display:flex;gap:4px;-webkit-app-region:no-drag}
button{font:inherit;color:var(--fg);background:rgba(255,255,255,.08);border:0;border-radius:8px;
  padding:6px 9px;cursor:pointer;font-size:13px;line-height:1}
button:hover{background:rgba(255,255,255,.16)}
button.on{background:var(--fg);color:#111}
#progress{position:fixed;top:0;left:0;height:2px;background:var(--accent);z-index:4;width:0}
main{position:relative;z-index:1;height:100%;overflow-y:auto;scroll-behavior:smooth;
  padding:42vh 22px 50vh;scrollbar-width:none;mask-image:linear-gradient(180deg,transparent 0,#000 18%,#000 80%,transparent)}
main::-webkit-scrollbar{display:none}
.line{max-width:760px;margin:0 auto;padding:12px 10px;border-radius:12px;cursor:pointer;
  opacity:.34;transition:opacity .35s,transform .35s;transform-origin:center;text-align:center}
.line:hover{opacity:.7;background:rgba(255,255,255,.04)}
.line.past{opacity:.24}
.line.active{opacity:1;transform:scale(1.04)}
.line.gap{height:18px;padding:0;cursor:default}
.he{direction:rtl;font-size:calc(30px*var(--size));font-weight:700;line-height:1.25;letter-spacing:-.01em}
.tr{font-size:calc(15px*var(--size));color:var(--tr);font-style:italic;margin-top:4px;opacity:.9}
.en{font-size:calc(19px*var(--size));color:var(--en);margin-top:5px;line-height:1.35}
.shaky::after{content:"•";color:var(--accent);margin-left:6px;font-style:normal}
body.no-he .he,body.no-tr .tr,body.no-en .en{display:none}
body.no-he .en{font-size:calc(28px*var(--size));color:var(--fg);font-weight:700}
#msg{position:fixed;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;
  gap:10px;z-index:2;text-align:center;padding:30px;pointer-events:none}
#msg .big{font-size:22px;font-weight:700}
#msg .small{color:var(--dim);font-size:14px;max-width:420px;line-height:1.5}
#msg .acts{display:flex;gap:8px;justify-content:center;margin-top:16px;pointer-events:auto}
a.btn{color:var(--fg);background:rgba(255,255,255,.08);border-radius:8px;padding:8px 12px;font-size:13px;text-decoration:none}
a.btn:hover{background:rgba(255,255,255,.16)}
#msg .acts button{padding:8px 12px}
.spin{width:22px;height:22px;border:2px solid var(--faint);border-top-color:var(--accent);border-radius:50%;
  animation:sp 0.9s linear infinite}
@keyframes sp{to{transform:rotate(360deg)}}
footer{position:fixed;bottom:0;left:0;right:0;z-index:3;display:flex;gap:8px;align-items:center;
  padding:12px 16px;background:linear-gradient(0deg,rgba(10,9,14,.9),rgba(10,9,14,0));font-size:12px;color:var(--dim)}
footer .grow{flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
footer a{color:var(--dim);cursor:pointer;text-decoration:underline}
#picker{position:fixed;z-index:5;inset:auto 12px 56px 12px;max-height:60vh;overflow:auto;
  background:rgba(24,22,30,.97);border:1px solid var(--faint);border-radius:14px;padding:8px;display:none}
#picker.open{display:block}
#picker .c{padding:10px;border-radius:9px;cursor:pointer}
#picker .c:hover{background:rgba(255,255,255,.07)}
#picker .c b{font-size:14px}
#picker .c div{font-size:12px;color:var(--dim);margin-top:3px;direction:rtl;text-align:left;unicode-bidi:plaintext}
</style></head>
<body>
<div id="bg"></div><div id="progress"></div>
<header>
  <img id="art" alt="">
  <div id="meta"><div id="title">Shir · שיר</div><div id="artist">Waiting for Spotify…</div></div>
  <div class="ctl">
    <button onclick="ctl('previous')" title="Previous">⏮</button>
    <button id="pp" onclick="ctl('playpause')" title="Play/pause">⏯</button>
    <button onclick="ctl('next')" title="Next">⏭</button>
  </div>
</header>
<main id="lyrics"></main>
<div id="msg"></div>
<div id="picker"></div>
<footer>
  <div class="grow" id="source"></div>
  <button id="t-he" title="Hebrew">עב</button>
  <button id="t-tr" title="Transliteration">abc</button>
  <button id="t-en" title="English">EN</button>
  <button onclick="size(-.1)" title="Smaller">A−</button>
  <button onclick="size(.1)" title="Bigger">A+</button>
</footer>
<script>
const $=s=>document.querySelector(s);
const store={get(k,d){try{const v=localStorage.getItem('shir.'+k);return v===null?d:JSON.parse(v)}catch(e){return d}},
             set(k,v){try{localStorage.setItem('shir.'+k,JSON.stringify(v))}catch(e){}}};
let prefs=store.get('prefs',{he:true,tr:true,en:true,size:1});
function applyPrefs(){for(const k of['he','tr','en']){document.body.classList.toggle('no-'+k,!prefs[k]);$('#t-'+k).classList.toggle('on',prefs[k])}
  document.documentElement.style.setProperty('--size',prefs.size);store.set('prefs',prefs)}
for(const k of['he','tr','en'])$('#t-'+k).onclick=()=>{prefs[k]=!prefs[k];applyPrefs()};
function size(d){prefs.size=Math.min(1.8,Math.max(.6,+(prefs.size+d).toFixed(2)));applyPrefs()}
applyPrefs();

let lyricStart=0,now=null,pos0=0,t0=0,trackId=null,data=null,lineEls=[],active=-1,userScrollUntil=0,lyricTimer=null;
async function ctl(action,value){await fetch('/api/control',{method:'POST',body:JSON.stringify({action,value})});setTimeout(poll,150)}
function position(){if(!now)return 0;return now.state==='playing'?pos0+(performance.now()-t0)/1000:pos0}
function msg(big,small,spin){$('#msg').innerHTML=(spin?'<div class="spin"></div>':'')+(big?`<div class="big">${esc(big)}</div>`:'')+(small?`<div class="small">${small}</div>`:'')}
function esc(s){return (s??'').toString().replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}

async function poll(){
  try{now=await (await fetch('/api/now')).json()}catch(e){now=null;msg('Shir isn\'t running','Start it again with python3 shir.py');return}
  pos0=now.position||0;t0=performance.now();
  if(!now.id){
    trackId=null;data=null;$('#lyrics').innerHTML='';
    const m={not_running:['Open Spotify','Play a song and the lyrics show up here.'],stopped:['Nothing playing','Play a song in Spotify.'],
      error:['Can\'t reach Spotify',esc(now.error||'')+'<br><br>On first run, macOS asks to let Terminal control Spotify. Allow it in System Settings → Privacy &amp; Security → Automation.']}[now.state]||['…',''];
    msg(m[0],m[1]);$('#title').textContent='Shir · שיר';$('#artist').textContent='Waiting for Spotify…';return}
  $('#pp').textContent=now.state==='playing'?'⏸':'▶';
  if(now.id!==trackId){
    trackId=now.id;data=null;active=-1;lyricStart=performance.now();$('#lyrics').innerHTML='';$('#picker').classList.remove('open');
    $('#title').textContent=now.title;$('#artist').textContent=now.artist;
    if(now.artwork){$('#art').src=now.artwork;$('#bg').style.backgroundImage=`url("${now.artwork}")`}else{$('#art').removeAttribute('src');$('#bg').style.backgroundImage=''}
    document.title=now.title+' · Shir';loadLyrics();
  }
}
async function loadLyrics(extra=''){
  clearTimeout(lyricTimer);const id=trackId;
  const r=await (await fetch('/api/lyrics'+extra)).json();
  if(id!==trackId)return;
  if(r.status==='working'){const sec=Math.round((performance.now()-lyricStart)/1000);
    msg('',esc(r.stage)+(sec>=3?` <span style="opacity:.6">${sec}s</span>`:'')+(sec>=25?'<br><span style="opacity:.6">The translation server is slow right now. Hang on…</span>':''),true);
    lyricTimer=setTimeout(()=>loadLyrics(),900);return}
  data=r;render();
}
function render(){
  const L=$('#lyrics');L.innerHTML='';lineEls=[];active=-1;
  const cands=(data.candidates||[]).length?' · <a onclick="openPicker()">wrong lyrics?</a>':'';
  const paste=' · <a onclick="openPaste()">paste lyrics</a>'+(data.shironet?` · <a href="${esc(data.shironet)}" target="_blank" rel="noopener">search Shironet</a>`:'');
  if(data.status==='notfound'){msg('No lyrics found','Not on LRCLIB or lyrics.ovh. Find them on Shironet, copy them, and paste them here. Shir will translate them.'+
      '<div class="acts">'+(data.shironet?`<a class="btn" href="${esc(data.shironet)}" target="_blank" rel="noopener">Search Shironet ↗</a>`:'')+'<button class="on" onclick="openPaste()">Paste lyrics</button></div>');
    $('#source').innerHTML=(cands.slice(3)+paste).replace(/^ · /,'');return}
  if(data.status==='instrumental'){msg('Instrumental ♪','');$('#source').innerHTML='';return}
  if(data.status==='error'){msg('Something went wrong',esc(data.error));$('#source').innerHTML='<a onclick="loadLyrics(\'?refresh=1\')">retry</a>';return}
  if(data.status!=='ok')return;
  msg('','');
  data.lines.forEach((l,i)=>{
    const d=document.createElement('div');
    if(!l.he){d.className='line gap'}else{
      d.className='line';
      const shaky=l.ok!==undefined&&l.ok<0.4;
      d.innerHTML=`<div class="he">${esc(l.he)}</div>`+(l.tr?`<div class="tr">${esc(l.tr)}</div>`:'')+
        (l.en&&l.en!==l.he?`<div class="en${shaky?' shaky':''}" ${shaky?'title="Jev thinks this translation may be off"':''}>${esc(l.en)}</div>`:'');
      if(l.t!=null)d.onclick=()=>ctl('seek',Math.max(0,l.t-0.3));
    }
    L.appendChild(d);lineEls.push(d);
  });
  const m=data.match||{};let how='';
  if(m.by==='jev')how=` · matched by Jev (${Math.round((m.probability||0)*100)}%)`;else if(m.by)how=' · '+m.by;
  $('#source').innerHTML=`Lyrics: ${esc(data.provider||'LRCLIB')}${data.synced?'':' (not time-synced)'}${how}`+(data.warning?' · '+esc(data.warning):'')+cands+
    ` · <a onclick="loadLyrics('?refresh=1')">refresh</a>`+paste;
}
function openPicker(){
  const p=$('#picker');
  p.innerHTML='<div style="padding:8px 10px;font-size:12px;color:var(--dim)">Pick the right song:</div>'+(data.candidates||[]).map(c=>
    `<div class="c" onclick="pick(${c.id})"><b>${esc(c.title)}</b> — ${esc(c.artist)} <span style="color:var(--dim);font-size:12px">${Math.floor((c.duration||0)/60)}:${String(Math.round((c.duration||0)%60)).padStart(2,'0')}${c.synced?' · synced':''}</span><div>${esc(c.preview)}</div></div>`).join('');
  p.classList.toggle('open');
}
function openPaste(){
  const p=$('#picker');
  p.innerHTML='<div style="padding:8px 10px;font-size:12px;color:var(--dim)">Paste the lyrics (from Shironet, Genius, anywhere). Shir will translate them.</div>'+
    '<textarea id="pastebox" dir="rtl" style="width:100%;height:40vh;background:rgba(0,0,0,.3);color:var(--fg);border:1px solid var(--faint);border-radius:10px;padding:10px;font:inherit;font-size:15px"></textarea>'+
    '<div style="display:flex;gap:8px;justify-content:flex-end;margin-top:8px"><button onclick="$(\'#picker\').classList.remove(\'open\')">Cancel</button><button class="on" onclick="sendPaste()">Use these lyrics</button></div>';
  p.classList.add('open');setTimeout(()=>$('#pastebox').focus(),50);
}
async function sendPaste(){
  const text=$('#pastebox').value.trim();if(!text)return;
  $('#picker').classList.remove('open');$('#lyrics').innerHTML='';msg('','Translating…',true);
  await fetch('/api/paste',{method:'POST',body:JSON.stringify({text})});loadLyrics();
}
function pick(id){$('#picker').classList.remove('open');$('#lyrics').innerHTML='';loadLyrics('?pick='+id)}

$('#lyrics').addEventListener('wheel',()=>userScrollUntil=performance.now()+4000,{passive:true});
$('#lyrics').addEventListener('touchmove',()=>userScrollUntil=performance.now()+4000,{passive:true});
function tick(){
  requestAnimationFrame(tick);
  if(!now||!now.duration)return;
  const p=position();$('#progress').style.width=Math.min(100,p/now.duration*100)+'%';
  if(!data||data.status!=='ok'||!lineEls.length)return;
  let idx=-1;
  if(data.synced){for(let i=0;i<data.lines.length;i++){if(data.lines[i].t!=null&&data.lines[i].t<=p+0.25)idx=i;else if(data.lines[i].t>p+0.25)break}}
  else{idx=Math.floor(p/now.duration*lineEls.length)}
  if(idx===active)return;active=idx;
  lineEls.forEach((el,i)=>{el.classList.toggle('active',data.synced&&i===idx);el.classList.toggle('past',data.synced&&i<idx)});
  if(idx>=0&&performance.now()>userScrollUntil){const el=lineEls[idx];const L=$('#lyrics');
    L.scrollTo({top:el.offsetTop-L.clientHeight*0.42+el.offsetHeight/2,behavior:'smooth'})}
}
poll();setInterval(poll,1000);requestAnimationFrame(tick);
fetch('/api/status').then(r=>r.json()).then(s=>{if(!s.translation)console.log('No DeepSeek key: showing Hebrew only')});
document.addEventListener('keydown',e=>{if(e.target.tagName==='INPUT'||e.target.tagName==='TEXTAREA')return;
  if(e.key===' '){e.preventDefault();ctl('playpause')}else if(e.key==='ArrowRight')ctl('next');else if(e.key==='ArrowLeft')ctl('previous');
  else if(e.key==='e')$('#t-en').click();else if(e.key==='t')$('#t-tr').click();else if(e.key==='h')$('#t-he').click()});
</script></body></html>"""

# ----------------------------------------------------------------------------- main

def main():
    global PLAYER
    ap = argparse.ArgumentParser(description="Live Hebrew/English lyrics for Spotify")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--float", action="store_true", help="always-on-top window (needs pywebview)")
    ap.add_argument("--no-open", action="store_true", help="don't open a browser")
    ap.add_argument("--demo", nargs="+", metavar=("TITLE", "ARTIST"),
                    help="fake player: --demo TITLE ARTIST [ALBUM] [SECONDS]")
    a = ap.parse_args()

    if a.demo:
        d = a.demo + [""] * 4
        PLAYER = DemoPlayer(d[0], d[1], d[2], float(d[3] or 0))
        if not d[3]:
            hit = next(iter(lrclib_search(track_name=d[0], artist_name=d[1])), None)
            if hit:
                PLAYER.set_duration(hit.get("duration"))
    elif sys.platform == "darwin":
        PLAYER = SpotifyPlayer()
    else:
        sys.exit("Shir reads Spotify through AppleScript, so it needs macOS. Try --demo to preview.")

    HOME.mkdir(parents=True, exist_ok=True)
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    url = f"http://127.0.0.1:{a.port}/"
    print(f"♪ Shir {VERSION} running at {url}")
    print(f"  translation: {'DeepSeek (' + CFG['llm_model'] + ')' if CFG['deepseek_api_key'] else 'off (no DEEPSEEK_API_KEY)'}")
    print(f"  Jev:         {('on (TypeSafe)' if CFG['typesafe_api_key'] else 'on (OpenRouter)') if jev_on() else 'off'}")
    print("  Ctrl+C to quit")

    if a.float:
        try:
            import webview  # pywebview
        except ImportError:
            sys.exit("--float needs pywebview:  pip3 install pywebview")
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        webview.create_window("Shir", url, width=440, height=760, on_top=True, background_color="#0e0d12")
        webview.start()
        return
    if not a.no_open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye ♪")


if __name__ == "__main__":
    main()
