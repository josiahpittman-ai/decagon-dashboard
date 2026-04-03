# Decagon Stats Dashboard — Setup Guide

## What this does

A lightweight Flask dashboard deployed to **Render** that shows three daily metrics pulled from Decagon's REST API:

| Metric | How it's calculated |
|---|---|
| **Deflection rate** | Conversations where `undeflected=false` (AI handled) / total |
| **Category %** | From Insights `tags` on each conversation |
| **CSAT average** | Mean of `csat` field (1–5) across conversations |

Everything comes from a single API call: `GET /conversation/export` with Unix timestamp filtering.

The dashboard auto-refreshes daily at 06:00 UTC. You can also hit **Refresh Now** or POST to `/api/refresh`.

---

## What you need from Decagon

### 1. API Key

Go to **decagon.ai/admin/settings/integrations** (or Settings > Security) and copy your API key.
This key goes into the `DECAGON_API_KEY` environment variable.

### 2. Test the endpoint

Run this curl to confirm your key works:

```bash
curl -s -H "Authorization: Bearer YOUR_API_KEY" \
  "https://api.decagon.ai/conversation/export?min_timestamp=$(date -d '1 day ago' +%s)&max_timestamp=$(date +%s)&page_size=5" \
  | python3 -m json.tool
```

You should see a JSON response with `"conversations": [...]` and `"next_cursor": ...`.

**If you're on the EU region**, use `https://eu.api.decagon.ai` instead and set `DECAGON_API_BASE=https://eu.api.decagon.ai` in your env vars.

---

## Deploy to Render

### Option A: Blueprint (recommended)

1. Push this folder to a GitHub repo
2. Go to [render.com](https://render.com) → **New** → **Blueprint**
3. Select the repo — Render reads `render.yaml` and creates the service
4. In the Render dashboard, go to **Environment** and enter your `DECAGON_API_KEY`
5. Deploy!

### Option B: Manual Web Service

1. Push to GitHub
2. Render → **New** → **Web Service** → connect the repo
3. Settings:
   - **Build command**: `pip install -r requirements.txt`
   - **Start command**: `gunicorn app:app --bind 0.0.0.0:$PORT --workers 2`
4. Add environment variables:
   - `DECAGON_API_KEY` = your key
   - `DECAGON_API_BASE` = `https://api.decagon.ai` (default, or `https://eu.api.decagon.ai` for EU)
5. Deploy!

---

## Run locally

```bash
cd decagon-dashboard
pip install -r requirements.txt
export DECAGON_API_KEY="your-key-here"
python app.py
# Open http://localhost:5000
```

---

## API response fields used

From `GET /conversation/export`, each conversation object provides:

| Field | Used for |
|---|---|
| `undeflected` (bool) | Deflection rate — `false` = AI deflected, `true` = escalated |
| `destination` (string) | Fallback — `"AI"` or `"AGENT"` |
| `tags` (array) | Category breakdown — `[{"name": "Billing", "level": 0}]` |
| `all_tags` (object) | Fallback tag source (grouped by hierarchy) |
| `csat` (int 1–5) | CSAT score |
| `csat_role` (string) | Whether AI or agent collected the rating |
| `csat_resolved` (bool) | Whether customer marked issue resolved |
| `customer_feedback` (string) | Free-text feedback |

---

## Endpoints

| Route | Method | Description |
|---|---|---|
| `/` | GET | Dashboard UI |
| `/api/stats` | GET | Raw JSON stats (great for integrations) |
| `/api/refresh` | POST | Force a manual data refresh |

---

## Customization tips

- **Change refresh schedule**: In `app.py`, edit the `scheduler.add_job(...)` line.
- **Add more metrics**: Extend `compute_stats()` — NPS, watchtower reviews, flow_type breakdown are all available in the export.
- **Feed into Google Sheets later**: Hit `/api/stats` from a Google Apps Script on a timer.
- **Filter by deflection**: Use `user_filters={"deflected_filter": true}` param to only pull deflected conversations.
