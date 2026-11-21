#!/usr/bin/env python3
"""Publisher: runs after the clip robot.

For every clip waiting in queue.json it:
  1. checks Telegram for Reject taps
  2. if the clip was rejected: marks it rejected and tells you
  3. if the reject window (30 min) is over: downloads it back from Telegram
     and posts it to every connected platform (YouTube, Facebook, Instagram),
     then tells you the links
  4. otherwise leaves it for the next run

A platform is "connected" when its secrets exist. One platform failing does not
stop the others, and a failed platform is not retried (so nothing is posted twice).

Needs a bot that nothing else is reading updates from (see README).
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

import requests

TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
QUEUE_FILE = Path(os.environ.get("QUEUE_FILE", "queue.json"))
WINDOW_MINUTES = int(os.environ.get("WINDOW_MINUTES", "30"))
MAX_UPLOADS_PER_DAY = int(os.environ.get("MAX_UPLOADS_PER_DAY", "6"))  # YouTube quota
YT_PRIVACY = os.environ.get("YT_PRIVACY", "public")  # public, unlisted or private
YT_CLIENT_ID = os.environ.get("YT_CLIENT_ID", "")
YT_CLIENT_SECRET = os.environ.get("YT_CLIENT_SECRET", "")
YT_REFRESH_TOKEN = os.environ.get("YT_REFRESH_TOKEN", "")
META_PAGE_ID = os.environ.get("META_PAGE_ID", "")          # Facebook Page id
META_PAGE_TOKEN = os.environ.get("META_PAGE_TOKEN", "")    # never-expiring Page token
META_IG_USER_ID = os.environ.get("META_IG_USER_ID", "")    # Instagram business id
GRAPH_VERSION = "v21.0"
GRAPH = f"https://graph.facebook.com/{GRAPH_VERSION}"
TG_FILE_LIMIT = 20 * 1024 * 1024  # bots cannot fetch bigger files


def log(*a):
    print(*a, flush=True)


# --------------------------------------------------------------- telegram
def tg(method, **kw):
    r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/{method}",
                      timeout=120, **kw)
    r.raise_for_status()
    return r.json()


def say(text):
    try:
        tg("sendMessage", data={"chat_id": TG_CHAT, "text": text,
                                "disable_web_page_preview": "true"})
    except Exception as e:
        log(f"telegram message failed: {e}")


def collect_rejections():
    """Return the set of clip ids you tapped Reject on, and clear those updates."""
    rejected, last_id = set(), None
    updates = tg("getUpdates", data={"timeout": 0,
                                     "allowed_updates": json.dumps(["callback_query"])})
    for u in updates.get("result", []):
        last_id = u["update_id"]
        cq = u.get("callback_query") or {}
        data = cq.get("data", "")
        if data.startswith("rej:"):
            rejected.add(data[4:])
            try:  # stops the button spinner; fails harmlessly if too old
                tg("answerCallbackQuery", data={"callback_query_id": cq["id"],
                                                "text": "Rejected"})
            except Exception:
                pass
    if last_id is not None:  # tell Telegram we have handled these
        tg("getUpdates", data={"offset": last_id + 1, "timeout": 0,
                               "allowed_updates": json.dumps(["callback_query"])})
    return rejected


def clear_button(message_id):
    try:
        tg("editMessageReplyMarkup", data={"chat_id": TG_CHAT, "message_id": message_id,
                                           "reply_markup": json.dumps({"inline_keyboard": []})})
    except Exception:
        pass


def fetch_video(file_id, dest):
    info = tg("getFile", data={"file_id": file_id})["result"]
    if info.get("file_size", 0) > TG_FILE_LIMIT:
        raise RuntimeError("video is over Telegram's 20MB limit for bots")
    r = requests.get(f"https://api.telegram.org/file/bot{TG_TOKEN}/{info['file_path']}",
                     timeout=180)
    r.raise_for_status()
    Path(dest).write_bytes(r.content)


# ---------------------------------------------------------------- youtube
def youtube_ready():
    return bool(YT_CLIENT_ID and YT_CLIENT_SECRET and YT_REFRESH_TOKEN)


def upload_to_youtube(path, title, description):
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    creds = Credentials(None, refresh_token=YT_REFRESH_TOKEN,
                        token_uri="https://oauth2.googleapis.com/token",
                        client_id=YT_CLIENT_ID, client_secret=YT_CLIENT_SECRET,
                        scopes=["https://www.googleapis.com/auth/youtube.upload"])
    yt = build("youtube", "v3", credentials=creds, cache_discovery=False)
    body = {"snippet": {"title": title[:100], "description": description[:4900],
                        "categoryId": "24"},          # 24 = Entertainment
            "status": {"privacyStatus": YT_PRIVACY,
                       "selfDeclaredMadeForKids": False}}
    media = MediaFileUpload(str(path), mimetype="video/mp4", resumable=True)
    req = yt.videos().insert(part="snippet,status", body=body, media_body=media)
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    return f"https://youtube.com/shorts/{resp['id']}"


# --------------------------------------------------------------- facebook
def facebook_ready():
    return bool(META_PAGE_ID and META_PAGE_TOKEN)


def instagram_ready():
    return bool(META_IG_USER_ID and META_PAGE_TOKEN)


def graph_error(r):
    try:
        e = r.json().get("error", {})
        return f"{e.get('message', r.text[:200])} (code {e.get('code')})"
    except Exception:
        return r.text[:200]


def graph_post(url, **kw):
    r = requests.post(url, timeout=300, **kw)
    if not r.ok:
        raise RuntimeError(graph_error(r))
    return r.json()


def upload_to_facebook(path, title, description):
    """Post the video as a Reel on the Facebook Page (start, upload, finish)."""
    size = Path(path).stat().st_size
    start = graph_post(f"{GRAPH}/{META_PAGE_ID}/video_reels",
                       data={"upload_phase": "start", "access_token": META_PAGE_TOKEN})
    video_id, upload_url = start["video_id"], start["upload_url"]
    with open(path, "rb") as f:
        r = requests.post(upload_url, data=f, timeout=600, headers={
            "Authorization": f"OAuth {META_PAGE_TOKEN}",
            "offset": "0", "file_size": str(size)})
    if not r.ok:
        raise RuntimeError("upload step: " + graph_error(r))
    graph_post(f"{GRAPH}/{META_PAGE_ID}/video_reels", data={
        "upload_phase": "finish", "video_id": video_id, "video_state": "PUBLISHED",
        "description": f"{title}\n\n{description}"[:2000],
        "access_token": META_PAGE_TOKEN})
    return f"https://www.facebook.com/reel/{video_id}"


# -------------------------------------------------------------- instagram
def instagram_container_and_upload(path, title, description):
    """Make a Reel container and send the video into it. Returns the container id."""
    size = Path(path).stat().st_size
    container = graph_post(f"{GRAPH}/{META_IG_USER_ID}/media", data={
        "media_type": "REELS", "upload_type": "resumable", "share_to_feed": "true",
        "caption": f"{title}\n\n{description}"[:2200],
        "access_token": META_PAGE_TOKEN})
    cid = container["id"]
    with open(path, "rb") as f:
        r = requests.post(f"https://rupload.facebook.com/ig-api-upload/{GRAPH_VERSION}/{cid}",
                          data=f, timeout=600, headers={
                              "Authorization": f"OAuth {META_PAGE_TOKEN}",
                              "offset": "0", "file_size": str(size)})
    if not r.ok:
        raise RuntimeError("upload step: " + graph_error(r))
    return cid


def upload_to_instagram(path, title, description):
    """Post the video as a Reel on the Instagram business account.
    Meta sometimes fails the upload step at random, so try up to 3 times,
    each time with a fresh container."""
    cid, last_error = None, None
    for attempt in range(1, 4):
        try:
            cid = instagram_container_and_upload(path, title, description)
            break
        except RuntimeError as e:
            last_error = e
            log(f"  Instagram upload attempt {attempt} failed: {e}")
            if attempt < 3:
                time.sleep(20 * attempt)
    if cid is None:
        raise last_error
    for _ in range(40):  # Instagram needs a moment to process the video
        time.sleep(8)
        s = requests.get(f"{GRAPH}/{cid}", timeout=60, params={
            "fields": "status_code,status", "access_token": META_PAGE_TOKEN}).json()
        code = s.get("status_code")
        if code == "FINISHED":
            break
        if code in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Instagram processing {code}: {s.get('status')}")
    else:
        raise RuntimeError("Instagram took too long to process the video")
    pub = graph_post(f"{GRAPH}/{META_IG_USER_ID}/media_publish", data={
        "creation_id": cid, "access_token": META_PAGE_TOKEN})
    try:
        link = requests.get(f"{GRAPH}/{pub['id']}", timeout=60, params={
            "fields": "permalink", "access_token": META_PAGE_TOKEN}).json().get("permalink")
    except Exception:
        link = None
    return link or f"Instagram post id {pub['id']}"


# ------------------------------------------------------------------- main
def load_queue():
    try:
        q = json.loads(QUEUE_FILE.read_text())
        q.setdefault("items", [])
        return q
    except Exception:
        return {"items": []}


def any_platform_ready():
    return youtube_ready() or facebook_ready() or instagram_ready()


def post_everywhere(item, youtube_allowed):
    """Download the clip once, post it to each connected platform.
    Returns (results, errors), both dicts keyed by platform name."""
    results, errors = {}, {}
    jobs = []
    if youtube_ready() and youtube_allowed:
        jobs.append(("YouTube", upload_to_youtube))
    if facebook_ready():
        jobs.append(("Facebook", upload_to_facebook))
    if instagram_ready():
        jobs.append(("Instagram", upload_to_instagram))
    with tempfile.TemporaryDirectory() as d:
        path = Path(d, "clip.mp4")
        fetch_video(item["file_id"], path)
        for name, fn in jobs:
            try:
                results[name] = fn(path, item["title"], item["description"])
            except Exception as e:
                errors[name] = f"{type(e).__name__}: {str(e)[:250]}"
                log(f"  {name} failed: {errors[name]}")
    return results, errors


def main(now=None):
    now = now or time.time()
    if not (TG_TOKEN and TG_CHAT):
        log("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing")
        return 1

    q = load_queue()
    pending = [i for i in q["items"] if i["status"] == "pending"]
    if not pending:
        log("nothing waiting")
        return 0

    rejected = collect_rejections()
    youtube_today = sum(1 for i in q["items"]
                        if i.get("youtube_id") and now - i.get("published_at", 0) < 86400)

    for item in pending:
        cid = item["id"]
        age_min = (now - item["sent_at"]) / 60
        if cid in rejected:
            item["status"] = "rejected"
            clear_button(item["message_id"])
            say(f"Rejected. Will not post: {item['title']}")
            log(f"{cid}: rejected")
        elif age_min < WINDOW_MINUTES:
            log(f"{cid}: still in reject window ({age_min:.0f} min old)")
        elif not any_platform_ready():
            log(f"{cid}: no platform connected yet, leaving it")
            if not q.get("warned_no_youtube"):
                say("A clip finished its reject window, but no platform is connected "
                    "yet, so nothing was posted. Add the YT_ or META_ secrets (see README).")
                q["warned_no_youtube"] = True
        else:
            youtube_allowed = youtube_today < MAX_UPLOADS_PER_DAY
            try:
                results, errors = post_everywhere(item, youtube_allowed)
            except Exception as e:  # could not even fetch the video
                results, errors = {}, {"Download": f"{type(e).__name__}: {str(e)[:250]}"}
            if youtube_ready() and not youtube_allowed:
                errors["YouTube"] = f"daily limit ({MAX_UPLOADS_PER_DAY}) reached, skipped"
            clear_button(item["message_id"])
            if results:
                item.update(status="published", published_at=int(now), posted=results)
                if "YouTube" in results:
                    item["youtube_id"] = results["YouTube"].rsplit("/", 1)[-1]
                    youtube_today += 1
                lines = [f"Posted: {item['title']}"]
                lines += [f"{n}: {link}" for n, link in results.items()]
                if errors:
                    lines += ["", "Did not work:"]
                    lines += [f"{n}: {err}" for n, err in errors.items()]
                say("\n".join(lines))
                log(f"{cid}: published {sorted(results)}")
            else:
                item["status"] = "failed"
                lines = [f"Could not post '{item['title']}':"]
                lines += [f"{n}: {err}" for n, err in errors.items()]
                say("\n".join(lines))
                log(f"{cid}: failed {errors}")

    QUEUE_FILE.write_text(json.dumps(q, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
