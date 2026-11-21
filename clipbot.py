#!/usr/bin/env python3
"""Twitch clip robot for Efe Clips Naija.

Finds Cater Efe's best new Twitch clips, turns each into a captioned vertical
(9:16) video, writes a title + description, and sends it to your Telegram with a
Reject button. publisher.py then posts the ones you did not reject.

Two ways to find clips (it picks automatically):
  1. Twitch API    - used when TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET are set
  2. Backup mode   - no key needed; yt-dlp reads his public clips page

Usage:
  python clipbot.py                  normal run (what GitHub Actions does)
  python clipbot.py --check          only test that Twitch clips can be found
  python clipbot.py --test-video in.mp4 out.mp4   build one video locally
"""
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

# ---------------------------------------------------------------- settings
CHANNEL = os.environ.get("TWITCH_CHANNEL", "carterefe")      # twitch.tv/<this>
CREDIT = os.environ.get("CREDIT_NAME", "Carter Efe")         # name shown in video
MAX_PER_RUN = int(os.environ.get("MAX_PER_RUN", "2"))        # videos per run
MIN_SECONDS = int(os.environ.get("MIN_SECONDS", "12"))       # skip tiny clips
MAX_AGE_HOURS = int(os.environ.get("MAX_AGE_HOURS", "720"))  # look back 30 days
CAPTIONS = os.environ.get("CAPTIONS", "1") == "1"
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "small")     # base < small < medium
WHISPER_LANGUAGE = os.environ.get("WHISPER_LANGUAGE", "en")  # "" = auto-detect
# a hint that makes Whisper expect Nigerian Pidgin words instead of guessing wrong ones
WHISPER_PROMPT = os.environ.get("WHISPER_PROMPT", (
    "Nigerian Pidgin and Nigerian English streamer. Words: abeg, wahala, oga, "
    "na so, wetin, dey, no be, I no know, how far, e don do, sha, abi, chai, "
    "omo, shey, biko, Carter Efe, Twitch, TikTok, Lagos."))
CAPTION_WORDS = int(os.environ.get("CAPTION_WORDS", "3"))     # words on screen at once
CAPTION_SIZE = int(os.environ.get("CAPTION_SIZE", "92"))      # text height in pixels
CAPTION_MARGIN = int(os.environ.get("CAPTION_MARGIN", "420")) # pixels up from the bottom
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"              # build but don't send
STATE_FILE = Path(os.environ.get("STATE_FILE", "seen.json"))
QUEUE_FILE = Path(os.environ.get("QUEUE_FILE", "queue.json"))
WINDOW_MINUTES = int(os.environ.get("WINDOW_MINUTES", "30"))  # time to reject
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
TWITCH_ID = os.environ.get("TWITCH_CLIENT_ID", "")
TWITCH_SECRET = os.environ.get("TWITCH_CLIENT_SECRET", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-5-5")

LAST_TRANSCRIPT = ""


FONT_CANDIDATES = [
    os.environ.get("FONT_FILE", ""),
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]


def log(*a):
    print(*a, flush=True)


# ------------------------------------------------------------------- state
def load_seen():
    try:
        return set(json.loads(STATE_FILE.read_text()))
    except Exception:
        return set()


def save_seen(seen):
    STATE_FILE.write_text(json.dumps(sorted(seen), indent=1))


# ------------------------------------------------------------------ twitch
def parse_time(value):
    """Return an aware datetime from an ISO string or a unix timestamp, else None."""
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value, tz=timezone.utc)
        when = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return when if when.tzinfo else when.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def as_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def fetch_clips_api():
    """Official way: Twitch Helix API (needs TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET)."""
    r = requests.post("https://id.twitch.tv/oauth2/token", timeout=30, data={
        "client_id": TWITCH_ID, "client_secret": TWITCH_SECRET,
        "grant_type": "client_credentials"})
    r.raise_for_status()
    headers = {"Client-Id": TWITCH_ID,
               "Authorization": f"Bearer {r.json()['access_token']}"}
    u = requests.get("https://api.twitch.tv/helix/users", timeout=30, headers=headers,
                     params={"login": CHANNEL})
    u.raise_for_status()
    users = u.json().get("data", [])
    if not users:
        raise RuntimeError(f"Twitch has no channel called '{CHANNEL}'")
    since = (datetime.now(timezone.utc) - timedelta(hours=MAX_AGE_HOURS)
             ).strftime("%Y-%m-%dT%H:%M:%SZ")
    c = requests.get("https://api.twitch.tv/helix/clips", timeout=30, headers=headers,
                     params={"broadcaster_id": users[0]["id"], "first": 100,
                             "started_at": since})
    c.raise_for_status()
    return [{"id": str(x["id"]), "title": (x.get("title") or "").strip(),
             "views": as_float(x.get("view_count")),
             "duration": as_float(x.get("duration")),
             "created": parse_time(x.get("created_at")),
             "url": x.get("url") or f"https://clips.twitch.tv/{x['id']}"}
            for x in c.json().get("data", [])]


def fetch_clips_backup():
    """No-key way: yt-dlp reads the public clips page. Less steady than the API,
    because Twitch can change that page without warning."""
    page = f"https://www.twitch.tv/{CHANNEL}/clips?filter=clips&range=30d"
    res = subprocess.run(["yt-dlp", "--flat-playlist", "-J", "--playlist-end", "60", page],
                         capture_output=True, text=True, timeout=180)
    if res.returncode != 0:
        raise RuntimeError("yt-dlp could not read the clips page: "
                           + (res.stderr.strip().splitlines() or ["unknown error"])[-1][:200])
    entries = json.loads(res.stdout).get("entries") or []
    clips = []
    for e in entries:
        if not e or not e.get("id"):
            continue
        clips.append({"id": str(e["id"]), "title": (e.get("title") or "").strip(),
                      "views": as_float(e.get("view_count")),
                      "duration": as_float(e.get("duration")),
                      "created": parse_time(e.get("timestamp") or e.get("upload_date")),
                      "url": e.get("webpage_url") or e.get("url")
                      or f"https://clips.twitch.tv/{e['id']}"})
    return clips


def fetch_clips():
    if TWITCH_ID and TWITCH_SECRET:
        try:
            clips = fetch_clips_api()
            log(f"Twitch API: {len(clips)} clips")
            return clips
        except Exception as e:
            log(f"Twitch API failed ({type(e).__name__}: {e}); trying backup mode")
    clips = fetch_clips_backup()
    log(f"Backup mode (yt-dlp): {len(clips)} clips")
    return clips


def pick_clips(clips, seen):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=MAX_AGE_HOURS)
    good = [c for c in clips
            if c["id"] not in seen
            and (c["created"] is None or c["created"] >= cutoff)
            and (c["duration"] == 0 or c["duration"] >= MIN_SECONDS)]
    good.sort(key=lambda c: c["views"], reverse=True)  # keeps page order if no view counts
    return good[:MAX_PER_RUN]


def download(clip_url, out_path):
    """yt-dlp turns a Twitch clip link into an mp4 file."""
    subprocess.run(["yt-dlp", "--no-playlist", "-f", "best[ext=mp4]/best",
                    "-o", str(out_path), clip_url], check=True, timeout=240,
                   capture_output=True)


# --------------------------------------------------------------- captions
def ass_time(t):
    cs = int(round(max(t, 0) * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02}:{s:02}.{cs:02}"


ASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,DejaVu Sans,{size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,1,0,0,0,100,100,0,0,1,{outline},3,2,60,60,{margin},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def build_ass(words, ass_path):
    """words = [(start, end, text), ...]. Writes pop-style captions: a few words
    at a time, big bold uppercase, the word being spoken in gold."""
    if not words:
        return False
    chunks, cur = [], []
    for w in words:
        if cur and (len(cur) >= CAPTION_WORDS or w[0] - cur[-1][1] > 0.8):
            chunks.append(cur)
            cur = []
        cur.append(w)
    if cur:
        chunks.append(cur)

    events = []
    for ci, chunk in enumerate(chunks):
        chunk_end = chunks[ci + 1][0][0] if ci + 1 < len(chunks) else chunk[-1][1] + 0.3
        chunk_end = min(chunk_end, chunk[-1][1] + 0.6)  # do not hang on screen in silence
        for wi, (start, end, _) in enumerate(chunk):
            nxt = chunk[wi + 1][0] if wi + 1 < len(chunk) else chunk_end
            nxt = max(nxt, start + 0.08)
            parts = []
            for j, (_, _, text) in enumerate(chunk):
                t = text.upper().replace("{", "(").replace("}", ")")
                parts.append(("{\\c&H0000D7FF&}" + t + "{\\c&H00FFFFFF&}") if j == wi else t)
            events.append(f"Dialogue: 0,{ass_time(start)},{ass_time(nxt)},Default,,0,0,0,,"
                          + " ".join(parts))

    header = ASS_HEADER.format(size=CAPTION_SIZE, outline=max(4, CAPTION_SIZE // 14),
                               margin=CAPTION_MARGIN)
    Path(ass_path).write_text(header + "\n".join(events) + "\n", encoding="utf-8")
    return True


def transcribe(video_path, ass_path):
    """Transcribe with Whisper (word timings) and write an .ass caption file.
    Returns True on success, False to skip captions."""
    if not CAPTIONS:
        return False
    try:
        from faster_whisper import WhisperModel
        model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
        kwargs = dict(vad_filter=True, word_timestamps=True, beam_size=5,
                      initial_prompt=WHISPER_PROMPT,
                      condition_on_previous_text=False)
        if WHISPER_LANGUAGE:
            kwargs["language"] = WHISPER_LANGUAGE
        segments, info = model.transcribe(str(video_path), **kwargs)
        words = []
        for seg in segments:
            for w in (seg.words or []):
                text = w.word.strip()
                if text:
                    words.append((w.start, w.end, text))
        log(f"  captions: {len(words)} words, language {getattr(info, 'language', '?')}")
        global LAST_TRANSCRIPT
        LAST_TRANSCRIPT = " ".join(w[2] for w in words)  # used by the live catcher for titles
        return build_ass(words, ass_path)
    except Exception as e:
        log(f"  captions skipped: {type(e).__name__}: {e}")
        return False


# ------------------------------------------------------------------ video
def find_font():
    for f in FONT_CANDIDATES:
        if f and Path(f).exists():
            return f
    return ""


def build_video(src, out, credit, srt=None):
    """Vertical 1080x1920: blurred background, clip in the middle, credit on top,
    captions at the bottom. Runs inside the folder that holds src/srt so the
    filter paths stay simple."""
    src, out = Path(src).resolve(), Path(out).resolve()
    work = src.parent
    (work / "credit.txt").write_text(f"{credit} on Twitch", encoding="utf-8")
    font = find_font()

    chain = ("[0:v]split[a][b];"
             "[a]scale=1080:1920:force_original_aspect_ratio=increase,"
             "crop=1080:1920,boxblur=28:6[bg];"
             "[b]scale=1080:-2[fg];"
             "[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[v0]")
    last = "v0"
    if font:
        chain += (f";[{last}]drawtext=fontfile={font}:textfile=credit.txt:"
                  "fontsize=46:fontcolor=white:borderw=4:bordercolor=black:"
                  "x=(w-text_w)/2:y=150[v1]")
        last = "v1"
    if srt:
        # the .ass file carries its own 1080x1920 pixel style
        chain += f";[{last}]subtitles={Path(srt).name}[v2]"
        last = "v2"

    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", src.name,
           "-filter_complex", chain, "-map", f"[{last}]", "-map", "0:a?",
           # bitrate cap keeps a 60s video under ~19MB, the most a bot can fetch back later
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "24",
           "-maxrate", "2500k", "-bufsize", "5000k",
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k",
           "-movflags", "+faststart", "-t", "60", str(out)]
    subprocess.run(cmd, check=True, cwd=work, timeout=600)


# ------------------------------------------------------------ title & text
def make_title(clip):
    fallback_title = (clip["title"] or f"{CREDIT} stream moment")[:70]
    desc = (f"Fan-run clips page (unofficial). Clip from {CREDIT}'s Twitch stream. "
            f"All credit to {CREDIT}.\n"
            f"Watch live: https://twitch.tv/{CHANNEL}\nOriginal clip: {clip['url']}\n"
            f"#{CHANNEL} #twitch #nigeria #shorts")
    if ANTHROPIC_KEY:
        try:
            r = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json={"model": ANTHROPIC_MODEL, "max_tokens": 300, "messages": [{
                    "role": "user",
                    "content": (
                        f"Write a YouTube Shorts title (max 70 characters, no quotes, "
                        f"curiosity-driven, must not mislead) for a clip of Nigerian "
                        f"streamer {CREDIT} on Twitch. The clip is called: "
                        f"'{clip['title']}'. Reply with only the title.")}]},
                timeout=40)
            r.raise_for_status()
            t = r.json()["content"][0]["text"].strip().strip('"')
            if t:
                fallback_title = t[:90]
        except Exception as e:
            log(f"  title via Claude skipped: {type(e).__name__}")
    return fallback_title, desc


# --------------------------------------------------------------- telegram
def tg(method, **kw):
    r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/{method}",
                      timeout=180, **kw)
    r.raise_for_status()
    return r.json()


def notify(text):
    if TG_TOKEN and TG_CHAT and not DRY_RUN:
        try:
            tg("sendMessage", data={"chat_id": TG_CHAT, "text": text})
        except Exception as e:
            log(f"telegram notify failed: {e}")


def send_video(path, title, desc, clip_id):
    """Send the video with a Reject button. Returns (message_id, file_id)."""
    note = (f"\n\nIt posts about {WINDOW_MINUTES} minutes after this "
            f"message unless you tap Reject.")
    caption = f"{title}\n\n{desc}"[:1000 - len(note)] + note
    markup = json.dumps({"inline_keyboard": [[
        {"text": "Reject (do not post)", "callback_data": f"rej:{clip_id}"}]]})
    with open(path, "rb") as f:
        res = tg("sendVideo", data={"chat_id": TG_CHAT, "caption": caption,
                                    "supports_streaming": "true",
                                    "reply_markup": markup},
                 files={"video": f})["result"]
    return res["message_id"], res["video"]["file_id"]


# ------------------------------------------------------------------ queue
def load_queue():
    try:
        q = json.loads(QUEUE_FILE.read_text())
        q.setdefault("items", [])
        return q
    except Exception:
        return {"items": []}


def save_queue(q):
    q["items"] = q["items"][-100:]  # keep the file small
    QUEUE_FILE.write_text(json.dumps(q, indent=1))


# ------------------------------------------------------------------- main
def main():
    if len(sys.argv) >= 4 and sys.argv[1] == "--test-video":
        build_video(sys.argv[2], sys.argv[3], CREDIT,
                    srt=sys.argv[4] if len(sys.argv) > 4 else None)
        log("built", sys.argv[3])
        return 0

    try:
        clips = fetch_clips()
    except Exception as e:
        msg = f"Clip robot could not read Twitch clips for '{CHANNEL}': {e}"
        log(msg)
        notify(msg)
        return 1
    if "--check" in sys.argv:
        for c in pick_clips(clips, set()):
            log(f"- {c['id']} ({int(c['views'])} views) {c['title']}")
        return 0

    if not DRY_RUN and not (TG_TOKEN and TG_CHAT):
        log("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing")
        return 1

    seen = load_seen()
    chosen = pick_clips(clips, seen)
    log(f"{len(chosen)} new clip(s) to process")
    if not chosen and os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch":
        # only for runs you start by hand, so the scheduled runs stay quiet
        notify(f"Clip robot ran: Twitch returned {len(clips)} clips for '{CHANNEL}' but none "
               f"are new (already sent, older than {MAX_AGE_HOURS} hours, or shorter "
               f"than {MIN_SECONDS} seconds).")
    for clip in chosen:
        log(f"- {clip['id']} ({int(clip['views'])} views) {clip['title']}")
        try:
            with tempfile.TemporaryDirectory() as d:
                src, srt, out = Path(d, "in.mp4"), Path(d, "captions.ass"), Path(d, "out.mp4")
                download(clip["url"], src)
                has_srt = transcribe(src, srt)
                build_video(src, out, CREDIT, srt=srt if has_srt else None)
                title, desc = make_title(clip)
                if DRY_RUN:
                    keep = Path(f"dryrun_{clip['id']}.mp4")
                    keep.write_bytes(out.read_bytes())
                    log(f"  dry run saved {keep}")
                else:
                    message_id, file_id = send_video(out, title, desc, clip["id"])
                    log("  sent to Telegram")
                    q = load_queue()
                    q["items"].append({
                        "id": clip["id"], "title": title, "description": desc,
                        "message_id": message_id, "file_id": file_id,
                        "sent_at": int(time.time()), "status": "pending"})
                    save_queue(q)
            seen.add(clip["id"])
            save_seen(seen)
        except Exception as e:
            log(f"  failed: {type(e).__name__}: {e}")
            notify(f"Clip robot failed on clip {clip['id']}: {type(e).__name__}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
