# Efe Clips Naija: Cater Efe Twitch clip robot

Separate from the Peller robot. Own repo, own Telegram bot, own YouTube channel.

## What it does

**Clip robot** (`clips.yml`, every 5 hours, up to 2 clips per run)
1. finds Cater Efe's best new Twitch clips (channel `carterefe`)
2. makes a 9:16 video with his name on top and captions at the bottom
3. sends it to your Telegram with a **Reject** button

**Publisher** (`publish.yml`, 35 and 55 minutes after each clip run)
1. checks Telegram for Reject taps
2. rejected clips are dropped
3. clips older than 30 minutes that you did not reject are uploaded to YouTube
4. you get a Telegram message with the link

**Live catcher** (`live.yml`, `livebot.py`): catches moments while Cater Efe is streaming
1. every 15 minutes a quick check asks Twitch (through streamlink) if `carterefe` is live; nothing else runs if he is not
2. while live it records the stream in 6 second pieces and listens to the volume
3. when the last ~12 seconds are much louder than normal (shouting, laughing, hype) it cuts about 30 seconds before and 25 seconds after
4. the cut goes through the same captions, vertical video, Telegram Reject button and posting as the normal clips. Moments with no speech are skipped
5. 7 minutes minimum between clips, at most 4 clips per run
6. after each clip's 30 minute window it starts the publisher right away

Tuning (environment variables in `live.yml`): `SPIKE_DB` (lower = catches more, default 7),
`MIN_LOUD_DB`, `COOLDOWN_SECONDS`, `MAX_LIVE_CLIPS`, `PRE_SEGS` / `POST_SEGS`.
Test without a stream: `SOURCE_URL=video.mp4 DRY_RUN=1 LIVE_REQUIRE_SPEECH=0 python livebot.py`.

## Two ways to find clips

- **Twitch API** (steady): add `TWITCH_CLIENT_ID` and `TWITCH_CLIENT_SECRET` secrets.
- **Backup mode** (no key): used automatically when those secrets are missing, or if the
  API call fails. Reads his public clips page with yt-dlp. Twitch can change that page
  without warning, so this can break.

Test without posting: run the workflow by hand and read the log, or locally
`python clipbot.py --check`.

## Secrets (Settings > Secrets and variables > Actions)

| Secret | What |
|---|---|
| `TELEGRAM_BOT_TOKEN` | token of a NEW bot used only by this robot |
| `TELEGRAM_CHAT_ID` | your chat ID |
| `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET` | optional, from dev.twitch.tv/console |
| `ANTHROPIC_API_KEY` | optional, better titles |
| `YT_CLIENT_ID`, `YT_CLIENT_SECRET`, `YT_REFRESH_TOKEN` | YouTube upload, from `get_youtube_token.py` |

Until the `YT_` secrets exist, clips are sent to Telegram but never posted.

## Facebook and Instagram

The publisher posts each approved clip as a Reel to the Facebook Page and the linked
Instagram business account, in the same run as YouTube. Each platform is used only if its
secrets exist, and one failing does not stop the others.

| Secret | What |
|---|---|
| `META_PAGE_ID` | Facebook Page id |
| `META_PAGE_TOKEN` | never-expiring Page access token |
| `META_IG_USER_ID` | Instagram business account id |

Get all three by running `python get_meta_token.py` on your computer (see the steps in the
chat). TikTok comes later: it needs TikTok's approval before it can post publicly.

## Limits to know about

- New Google Cloud projects that have not passed Google's API audit can only upload
  videos as private. You can make them public by hand in YouTube Studio.
- The default YouTube quota allows about 6 uploads a day (`MAX_UPLOADS_PER_DAY`).
- 5-hour runs happen at 00, 05, 10, 15, 20 UTC, which is 01, 06, 11, 16, 21 in Nigeria.
- Captions use Whisper: good for English, weaker for Pidgin, Yoruba and slang.
- Message Cater Efe's team before posting, and always credit him (the robot already does).
