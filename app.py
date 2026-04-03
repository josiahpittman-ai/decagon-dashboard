"""
Decagon Stats Dashboard
-----------------------
A Flask app that pulls daily deflection rate, category percentages, and CSAT
scores from Decagon's REST API and displays them on a clean dashboard.

Deploy to Render as a Web Service.

API Reference:
  - GET /conversation/export  (with Unix timestamps)
  - Auth: Authorization: Bearer <api_key>
  - Base: https://api.decagon.ai  (US) or https://eu.api.decagon.ai (EU)
"""

import os
import json
import logging
from datetime import datetime, timedelta, timezone

import requests
from flask import Flask, render_template, jsonify
from apscheduler.schedulers.background import BackgroundScheduler

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DECAGON_API_KEY = os.environ.get("DECAGON_API_KEY", "")
DECAGON_API_BASE = os.environ.get("DECAGON_API_BASE", "https://api.decagon.ai")
PORT = int(os.environ.get("PORT", 5000))

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# In-memory cache for the latest stats (refreshed daily)
# ---------------------------------------------------------------------------
stats_cache = {
    "last_updated": None,
    "deflection_rate": None,
    "categories": {},
    "csat": {
        "average": None,
        "total_ratings": 0,
        "distribution": {},
    },
    "conversation_totals": {
        "total": 0,
        "deflected": 0,
        "escalated": 0,
    },
    "error": None,
}


# ---------------------------------------------------------------------------
# Decagon API helpers
# ---------------------------------------------------------------------------
def _headers():
    return {
        "Authorization": f"Bearer {DECAGON_API_KEY}",
        "Content-Type": "application/json",
    }


def export_all_conversations(min_ts: float, max_ts: float) -> list:
    """
    Paginated fetch from GET /conversation/export.

    Uses cursor-based pagination. Timestamps are Unix epoch floats.
    Returns the full list of conversation objects for the time range.
    """
    url = f"{DECAGON_API_BASE}/conversation/export"
    all_conversations = []
    cursor = None

    while True:
        params = {
            "timestamp_filter": "created_at",
            "min_timestamp": min_ts,
            "max_timestamp": max_ts,
            "page_size": 1000,
        }
        if cursor is not None:
            params["cursor"] = cursor

        try:
            resp = requests.get(url, headers=_headers(), params=params, timeout=60)
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as e:
            logger.error(f"Failed to export conversations: {e}")
            break

        conversations = data.get("conversations", [])
        all_conversations.extend(conversations)

        cursor = data.get("next_cursor")
        if not cursor or len(conversations) == 0:
            break

    logger.info(f"Fetched {len(all_conversations)} conversations")
    return all_conversations


# ---------------------------------------------------------------------------
# Stats computation
# ---------------------------------------------------------------------------
def compute_stats():
    """Fetch data from Decagon and recompute all dashboard metrics."""
    global stats_cache

    if not DECAGON_API_KEY:
        stats_cache["error"] = "DECAGON_API_KEY not set. Add it as an environment variable."
        logger.error(stats_cache["error"])
        return

    now = datetime.now(timezone.utc)
    yesterday = now - timedelta(days=1)
    min_ts = yesterday.timestamp()
    max_ts = now.timestamp()

    logger.info(
        f"Refreshing stats for {yesterday.strftime('%Y-%m-%d %H:%M')} "
        f"→ {now.strftime('%Y-%m-%d %H:%M')} UTC"
    )

    # -- 1. Pull conversations -------------------------------------------------
    conversations = export_all_conversations(min_ts, max_ts)

    total = len(conversations)
    deflected = 0      # AI handled, not escalated
    escalated = 0      # Handed off to a human
    category_counts = {}

    # CSAT extracted from conversation objects (no separate GET endpoint)
    csat_values = []
    csat_distribution = {}

    for convo in conversations:
        # ---- Deflection / Escalation ----
        # "undeflected" = true means escalated to a human agent
        # "undeflected" = false (or absent) means AI deflected it
        # "destination" is "AI" or "AGENT"
        is_undeflected = convo.get("undeflected", False)
        destination = convo.get("destination", "")

        if is_undeflected or destination == "AGENT":
            escalated += 1
        else:
            deflected += 1

        # ---- Categories (from Insights tags) ----
        tags = convo.get("tags", [])
        if tags:
            # Use the first (top-level) tag as the category
            cat_name = tags[0].get("name", "Uncategorized") if tags else "Uncategorized"
        else:
            # Fallback: check all_tags for any hierarchy
            all_tags = convo.get("all_tags", {})
            cat_name = "Uncategorized"
            for hierarchy_id, hierarchy in all_tags.items():
                hierarchy_tags = hierarchy.get("tags", [])
                if hierarchy_tags:
                    cat_name = hierarchy_tags[0].get("name", "Uncategorized")
                    break

        category_counts[cat_name] = category_counts.get(cat_name, 0) + 1

        # ---- CSAT (embedded in conversation object) ----
        csat_score = convo.get("csat")
        if csat_score is not None:
            try:
                csat_score = int(csat_score)
                csat_values.append(csat_score)
                key = str(csat_score)
                csat_distribution[key] = csat_distribution.get(key, 0) + 1
            except (ValueError, TypeError):
                pass

    # Deflection rate = deflected / total conversations
    deflection_rate = (
        round((deflected / total) * 100, 1) if total > 0 else 0.0
    )

    # Category percentages (sorted descending)
    category_pcts = {}
    if total > 0:
        for cat, count in sorted(category_counts.items(), key=lambda x: -x[1]):
            category_pcts[cat] = round((count / total) * 100, 1)

    # CSAT average
    csat_avg = round(sum(csat_values) / len(csat_values), 2) if csat_values else None

    # -- 2. Update cache -------------------------------------------------------
    stats_cache = {
        "last_updated": now.isoformat(),
        "date_range": f"{yesterday.strftime('%Y-%m-%d')} to {now.strftime('%Y-%m-%d')}",
        "deflection_rate": deflection_rate,
        "categories": category_pcts,
        "csat": {
            "average": csat_avg,
            "total_ratings": len(csat_values),
            "distribution": csat_distribution,
        },
        "conversation_totals": {
            "total": total,
            "deflected": deflected,
            "escalated": escalated,
        },
        "error": None,
    }
    logger.info(f"Stats refreshed: {json.dumps(stats_cache, indent=2)}")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/")
def dashboard():
    return render_template("dashboard.html", stats=stats_cache)


@app.route("/api/stats")
def api_stats():
    """JSON endpoint for programmatic access or JS fetch."""
    return jsonify(stats_cache)


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    """Manual refresh trigger."""
    compute_stats()
    return jsonify({"status": "ok", "stats": stats_cache})


# ---------------------------------------------------------------------------
# Scheduler: refresh daily at 6 AM UTC
# ---------------------------------------------------------------------------
scheduler = BackgroundScheduler()
scheduler.add_job(compute_stats, "cron", hour=6, minute=0)
scheduler.start()


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------
# Run initial fetch on startup
compute_stats()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=False)
