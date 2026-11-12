# Outlier Finder

Find short-form videos on **YouTube Shorts, TikTok and Instagram Reels** that massively outperformed their creator's normal views, then break down *why*: topic, format, hook, script sections, retention edits, and scores. It also ranks keywords by **high demand, low supply**.

## Quick start

```bash
cd outlier-finder
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # also install ffmpeg: brew/apt/winget install ffmpeg
uvicorn app.main:app --reload
# open http://localhost:8000 → Scans → "Scan all"
```

With no keys, every platform runs on **demo data** and analyses use a demo analyzer, so you can explore everything first. The bar under the header shows which sources are live.

## Data sources (switchable per platform in `niches.yaml`)

| Platform | `official` | `scraper` (Apify) |
|---|---|---|
| **YouTube Shorts** | YouTube Data API. Free, 10,000 units/day. **Default.** | n/a |
| **TikTok** | Not possible: TikTok has no official API for other creators' videos | **Default.** Hashtag/keyword search, profiles, views, likes, comments, shares, saves, followers, video files |
| **Instagram Reels** | Business Discovery: creators you add by handle (business/creator accounts), followers, likes, comments, Reel views. **No keyword search, no video files** | **Default.** Hashtag search, profiles, plays, likes, comments, followers, video files |

> ⚠️ Scrapers collect public data in ways TikTok's and Instagram's terms of service don't allow. That's common for personal research, but a real risk in a product you sell. Set `instagram: official` (and `tiktok: off`) to stay inside the rules.

**Keys** go in `outlier-finder/.env` (copy `.env.example`):

| Variable | Where to get it |
|---|---|
| `YOUTUBE_API_KEY` | [Google Cloud Console](https://console.cloud.google.com/) → enable *YouTube Data API v3* → Credentials → API key |
| `APIFY_TOKEN` | [Apify console](https://console.apify.com/) → Settings → Integrations. Uses the `clockworks/tiktok-scraper` and `apify/instagram-scraper` actors, billed per result |
| `IG_ACCESS_TOKEN`, `IG_USER_ID` | Only for `instagram: official`. A Meta app connected to your own Instagram business/creator account |
| `ANTHROPIC_API_KEY` | [Anthropic console](https://console.anthropic.com/) → API keys |

**Check the scraper fields on your first real run.** Apify actors change their output over time, and I couldn't test against live Apify from the build environment. If numbers come back empty, compare one item from the actor's output in the Apify console with the getters in `app/providers/apify.py`.

## What it measures

| Metric | How |
|---|---|
| **Outlier score** | views ÷ the creator's **median** views over their recent posts (Shorts compared with Shorts) |
| **Views ÷ followers** | e.g. 1M views on a 10k account = 100× |
| **Engagement rate** | (likes + comments + shares + saves) ÷ views. YouTube doesn't publish shares or saves |
| **Views per day** | views ÷ days since upload |
| **Followers gained (estimate)** | No platform shares this for other people's videos. The app takes a follower snapshot every time a creator is refreshed, then compares followers just before a post with 2–7 days after, subtracts the creator's normal daily growth, and splits the gain between posts in that window by views. **It needs history**, so track creators and refresh daily |
| **Keyword opportunity** | (outliers × median outlier score) ÷ log₁₀(recent uploads). Only YouTube publishes upload counts; TikTok/Instagram opportunity is demand-only |

## Pages

- **Outliers:** filter by platform, genre and keyword. Sort by outlier score, views ÷ followers, engagement, velocity, views or newest.
- **Opportunities:** keywords ranked by demand vs. supply, per platform.
- **Creators:** add creators by handle or URL on any platform (a watchlist, like Sandcastles). Shows follower growth, outlier count and best outlier. **Refresh** pulls new posts and takes a follower snapshot.
- **Video:** all metrics plus the **Script & format breakdown**.
- **Scans:** run keyword scans per genre and platform, and track progress and quota.

## Script & format breakdown

Click **Analyze this video** on any video page:

1. **Get the file.** Scraper links for TikTok/Instagram, with a fresh link requested if the stored one expired. For YouTube, `yt-dlp` if installed (against YouTube's terms). **Or upload the file** on the page, which works for every platform.
2. **Measure the edits** with ffmpeg: scene cuts (cuts per 10s, average shot length, cuts in the first 3s, longest shot) and up to 8 frames: the hook plus frames just after cuts.
3. **Transcribe** locally with [faster-whisper](https://github.com/SYSTRAN/faster-whisper), with word timestamps. This gives words per minute, time to first word, pauses and the hook text. The model downloads once from Hugging Face on first use. `analysis.whisper_model` sets the size.
4. **Claude** (`claude-opus-5-5`, effort `high`) gets the stats, edit metrics, timestamped transcript and frames. It returns schema-checked JSON:
   - topic, angle and the reusable **format**
   - the **hook**: spoken words, on-screen text, visual, type, promise
   - every **section** with timestamps (hook, setup, credibility, rehook, value beat, story, twist, payoff, proof, CTA, loop)
   - **retention techniques** with timestamp evidence
   - an **edit breakdown**
   - **10 scores** with evidence: hook, retention, value, payoff, novelty, emotion, shareability, clarity, CTA, replicability
   - **why it outperformed**
   - a fill-in-the-blanks **script template** plus topic ideas

   The system prompt is cached between videos, and refusals fall back automatically (`fallbacks: "default"`).

The video and audio are deleted after analysis. The sampled frames are kept and shown on the page.

## Scheduled jobs

```bash
python -m app.cli refresh                       # tracked creators: new posts + follower snapshot
python -m app.cli scan --genre fitness           # keyword scan on all searchable platforms
python -m app.cli scan --genre all --platform tiktok
```

Run `refresh` daily (cron: `15 7 * * * cd /path/outlier-finder && .venv/bin/python -m app.cli refresh`) to build the follower history behind the followers-gained estimates.

## Costs to expect

- **YouTube:** free within 10,000 units/day. All 39 keywords use about 4,000–6,000 units.
- **Apify:** per result. One TikTok or Instagram keyword is about `results_per_keyword` results, plus each new creator's recent posts. Creator data is cached for 7 days.
- **Claude:** one call per analysis, with roughly 8 images plus a transcript.

## Project layout

```
niches.yaml               genres, keywords, platforms, thresholds, analysis settings  ← edit this
app/providers/            youtube.py · apify.py (TikTok + Instagram) · instagram_official.py · fake.py (demo)
app/scanner.py            keyword scans, creator tracking, follower snapshots
app/scoring.py            outlier, engagement, opportunity, followers-gained estimate
app/media.py              download, ffmpeg cuts/frames/audio
app/transcribe.py         faster-whisper + script metrics
app/analyzer.py           Claude prompt, output schema, scoring rubric
app/pipeline.py           runs one analysis end to end
app/main.py · templates/  web app
app/cli.py                scheduled jobs
tests/                    pytest -q
```

## Next steps

- **Topic clustering:** group analyzed outliers into topics and formats across platforms, and rank formats by how often they produce outliers.
- **Sign in with YouTube / TikTok / Instagram** to personalize suggestions (your niche, your baseline, gap analysis). Each platform's login only shares the signed-in user's own data, including their real followers-gained per video.
- **Score calibration:** check that the analysis scores correlate with outlier score across 50+ videos, and tune the rubric until they do.
