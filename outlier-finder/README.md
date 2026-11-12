# Outlier Finder

Find YouTube Shorts from **small channels that massively outperformed**, in your genres (fitness, content creation, AI, finance), then rank keywords by **high demand, low supply**.

This is **Phase 1** (finding outliers). Later phases add the AI breakdown and scoring (see the roadmap below).

## Quick start

```bash
cd outlier-finder
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
# open http://localhost:8000 → Scans → "Scan all"
```

Without an API key the app runs in **demo mode** with synthetic data, so you can explore the UI right away.

### Use real YouTube data

1. Go to [Google Cloud Console](https://console.cloud.google.com/) → create a project.
2. **APIs & Services → Library →** enable **YouTube Data API v3**.
3. **Credentials → Create credentials → API key.** Restrict it to the YouTube Data API.
4. Create `outlier-finder/.env` (already gitignored):
   ```
   YOUTUBE_API_KEY=your-key-here
   ```
5. Restart the server, then run a scan from the **Scans** page.

**Quota:** the free tier is 10,000 units/day. Each keyword search costs 100 units, and each new channel costs about 2 more. Scanning all 39 keywords uses roughly 4,000–6,000 units. Channel data is cached for 7 days, so repeat scans are cheaper.

## How "outlier" is defined

| Metric | Formula | Why |
|---|---|---|
| **Outlier score** (main ranking) | views ÷ the channel's **median** views over its last 30 uploads (Shorts compared with Shorts) | Tells you whether the video beat *that creator's normal*, not just whether it's big |
| **Views ÷ subs** | views ÷ subscribers | Your original idea: 1M views on a 10k channel = 100× |
| **Views/day** | views ÷ days since upload | Fair comparison between old and new videos |

A video **qualifies** when the channel has 100–100k subs, the video has 10k+ views, it's ≤180s long, and its outlier score is ≥3×. All thresholds live in `niches.yaml`.

**Keyword opportunity** = (number of outliers × median outlier score) ÷ log₁₀(recent uploads matching the keyword). A high score means many strong outliers and not many people making that content.

## Project layout

```
niches.yaml          genres, keywords, thresholds   ← edit this
app/youtube.py       YouTube Data API client (quota tracking)
app/scanner.py       search → stats → channel medians → store → score
app/scoring.py       pure scoring functions
app/queries.py       read queries for pages and JSON API
app/demo.py          fake YouTube client (demo mode + tests)
app/main.py          FastAPI routes
app/templates/       Jinja pages
tests/               pytest (run: pytest -q)
```

JSON API: `GET /api/outliers?genre=fitness&sort=reach&limit=50`, `GET /api/opportunities`.

## Roadmap

**Phase 2: AI breakdown per outlier**
- Transcript (YouTube captions, or Whisper on the audio) + measured editing stats (ffmpeg scene cuts per 10s, on-screen text via OCR, words per minute, audio spikes) + sampled frames.
- An AI model returns structured JSON:
  - topic and angle
  - sections with timestamps (hook, setup, credibility, rehook/open loop, value beats, twist, payoff, CTA, loop)
  - retention edits
  - scores from 1–10, each with timestamp evidence: hook, retention/pacing, value, payoff, novelty, emotion, shareability, clarity, CTA, replicability
- Check that the scores correlate with outlier score, and tune the rubric until they do.

**Phase 3: topics and formats.** Cluster outliers into topics and reusable formats ("I tried X for 30 days"), and compute opportunity per topic, not just per keyword.

**Phase 4: accounts and personalization.** Users sign in with their platforms (see below).

## Signing in with YouTube, TikTok, Instagram

Yes, but each platform only shares **the signed-in user's own** data, not other people's videos.

| Platform | Sign-in | What you get | Approval needed |
|---|---|---|---|
| YouTube | Google OAuth (`youtube.readonly`, `yt-analytics.readonly`) | Their channel and videos, **plus retention curves and traffic sources** from YouTube Analytics | Google app verification before public launch |
| TikTok | Login Kit + Display API (`user.info.basic`, `video.list`) | Profile and their videos with view/like counts | TikTok app review |
| Instagram | Instagram API with Instagram Login (business/creator accounts) | Their Reels and insights (reach, plays, saves) | Meta app review |

**What personalization can do with that data:**
- Work out the user's niche from their own videos and pre-fill their keywords.
- Use **their own median** as the baseline, so suggestions are realistic for their channel size.
- **Gap analysis:** high-opportunity topics they haven't covered yet.
- Compare their retention curve against outliers in the same format, and score their hooks with the same rubric.

Finding *other* creators' outliers still comes from the YouTube Data API. TikTok and Instagram don't offer public search APIs for that.
