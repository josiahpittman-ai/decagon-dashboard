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
from flask import Flask, render_template, jsonify, request
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
    "start_date": None,
    "end_date": None,
    "date_range": None,
    "deflection_rate": None,
    "categories": {},
    "category_detail": [],       # Per-category/subcategory breakdown
    "error_analysis": [],        # Watchtower / Auto QA issues
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
def compute_stats(start_date: str = None, end_date: str = None):
    """
    Fetch data from Decagon and recompute all dashboard metrics.

    Args:        start_date: ISO date string (YYYY-MM-DD). Defaults to yesterday.
        end_date:   ISO date string (YYYY-MM-DD). Defaults to today.
    """
    global stats_cache

    if not DECAGON_API_KEY:
        stats_cache["error"] = "DECAGON_API_KEY not set. Add it as an environment variable."
        logger.error(stats_cache["error"])
        return

    now = datetime.now(timezone.utc)

    if start_date:
        start_dt = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    else:
        start_dt = now - timedelta(days=1)

    if end_date:
        # End of the selected day (23:59:59)
        end_dt = datetime.strptime(end_date, "%Y-%m-%d").replace(
            hour=23, minute=59, second=59, tzinfo=timezone.utc
        )
    else:
        end_dt = now

    min_ts = start_dt.timestamp()
    max_ts = end_dt.timestamp()

    logger.info(
        f"Refreshing stats for {start_dt.strftime('%Y-%m-%d')} "        f"→ {end_dt.strftime('%Y-%m-%d')} UTC"
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

    # Per-category/subcategory tracking:
    # key = (parent_category, subcategory_or_None)
    # value = {"total": int, "deflected": int, "escalated": int}
    cat_detail_map = {}

    # Watchtower / Auto QA error tracking
    error_items = []

    for convo in conversations:
        # ---- Deflection / Escalation ----
        is_undeflected = convo.get("undeflected", False)
        destination = convo.get("destination", "")
        is_escalated = is_undeflected or destination == "AGENT"

        if is_escalated:            escalated += 1
        else:
            deflected += 1

        # ---- Categories with subcategories (from tags array) ----
        tags = convo.get("tags", [])
        parent_cat = None
        subcategories = []

        if tags:
            for tag in tags:
                level = tag.get("level", 0)
                name = tag.get("name", "Uncategorized")
                if level == 0:
                    parent_cat = name
                else:
                    subcategories.append(name)
        else:
            # Fallback: check all_tags for any hierarchy
            all_tags = convo.get("all_tags", {})
            for hierarchy_id, hierarchy in all_tags.items():
                hierarchy_tags = hierarchy.get("tags", [])
                for tag in hierarchy_tags:
                    level = tag.get("level", 0)
                    name = tag.get("name", "Uncategorized")
                    if level == 0:
                        parent_cat = name
                    else:
                        subcategories.append(name)
                if parent_cat:
                    break

        if not parent_cat:
            parent_cat = "Uncategorized"

        # Track top-level category count (for the main dashboard donut)
        category_counts[parent_cat] = category_counts.get(parent_cat, 0) + 1

        # Track parent-level detail
        key_parent = (parent_cat, None)
        if key_parent not in cat_detail_map:
            cat_detail_map[key_parent] = {"total": 0, "deflected": 0, "escalated": 0}
        cat_detail_map[key_parent]["total"] += 1
        cat_detail_map[key_parent]["deflected"] += 0 if is_escalated else 1
        cat_detail_map[key_parent]["escalated"] += 1 if is_escalated else 0

        # Track each subcategory under this parent
        for sub in subcategories:
            key_sub = (parent_cat, sub)
            if key_sub not in cat_detail_map:
                cat_detail_map[key_sub] = {"total": 0, "deflected": 0, "escalated": 0}
            cat_detail_map[key_sub]["total"] += 1
            cat_detail_map[key_sub]["deflected"] += 0 if is_escalated else 1
            cat_detail_map[key_sub]["escalated"] += 1 if is_escalated else 0

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

        # ---- Watchtower / Auto QA Reviews ----
        watchtower = convo.get("watchtower_reviews", [])
        for review in watchtower:
            result = review.get("result", "")
            # Track all reviews, but flag failures/issues prominently
            rubric_score = review.get("rubric_score")
            rubric_review = review.get("rubric_review", {})

            # Collect failed rubric fields
            failed_fields = []
            for field_name, field_data in rubric_review.items():
                if isinstance(field_data, dict):
                    field_result = field_data.get("result", "")
                    if field_result.lower() in ("fail", "failed", "no", "false"):
                        failed_fields.append(field_name)

            error_items.append({
                "conversation_id": convo.get("id", ""),
                "job_name": review.get("job_name", "Unknown"),
                "result": result,
                "rationale": review.get("rationale", ""),
                "rubric_score": rubric_score,
                "failed_fields": failed_fields,
                "category": parent_cat,
                "created_at": convo.get("created_at", ""),
            })

    # Deflection rate = deflected / total conversations
    deflection_rate = (
        round((deflected / total) * 100, 1) if total > 0 else 0.0
    )

    # Category percentages (sorted descending)
    category_pcts = {}
    if total > 0:
        for cat, count in sorted(category_counts.items(), key=lambda x: -x[1]):
            category_pcts[cat] = round((count / total) * 100, 1)

    # Build category_detail list (sorted by parent, subcategories nested under)
    category_detail = []
    # Get unique parent categories sorted by total descending
    parents = sorted(
        {k[0] for k in cat_detail_map},
        key=lambda p: cat_detail_map.get((p, None), {}).get("total", 0),
        reverse=True,
    )
    for parent in parents:
        p_data = cat_detail_map.get((parent, None), {"total": 0, "deflected": 0, "escalated": 0})
        p_total = p_data["total"]
        category_detail.append({
            "category": parent,
            "subcategory": None,            "total": p_total,
            "deflected": p_data["deflected"],
            "escalated": p_data["escalated"],
            "deflection_rate": round((p_data["deflected"] / p_total) * 100, 1) if p_total > 0 else 0.0,
            "percentage": round((p_total / total) * 100, 1) if total > 0 else 0.0,
        })
        # Add subcategories for this parent
        sub_keys = sorted(
            [k for k in cat_detail_map if k[0] == parent and k[1] is not None],
            key=lambda k: cat_detail_map[k]["total"],
            reverse=True,
        )
        for sk in sub_keys:
            s_data = cat_detail_map[sk]
            s_total = s_data["total"]
            category_detail.append({
                "category": parent,
                "subcategory": sk[1],
                "total": s_total,
                "deflected": s_data["deflected"],
                "escalated": s_data["escalated"],
                "deflection_rate": round((s_data["deflected"] / s_total) * 100, 1) if s_total > 0 else 0.0,
                "percentage": round((s_total / total) * 100, 1) if total > 0 else 0.0,
            })

    # Sort error_items: failures first, then by rubric_score ascending
    error_items.sort(key=lambda x: (
        0 if x["result"].lower() in ("fail", "failed") else 1,
        x["rubric_score"] if x["rubric_score"] is not None else 999,
    ))

    # CSAT average
    csat_avg = round(sum(csat_values) / len(csat_values), 2) if csat_values else None

    # -- 2. Update cache -------------------------------------------------------
    stats_cache = {
        "last_updated": now.isoformat(),
        "date_range": f"{start_dt.strftime('%Y-%m-%d')} to {end_dt.strftime('%Y-%m-%d')}",
        "start_date": start_dt.strftime("%Y-%m-%d"),
        "end_date": end_dt.strftime("%Y-%m-%d"),
        "deflection_rate": deflection_rate,
        "categories": category_pcts,
        "category_detail": category_detail,
        "error_analysis": error_items,
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


@app.route("/categories")
def categories_page():
    return render_template("categories.html", stats=stats_cache)


@app.route("/api/stats")
def api_stats():
    """JSON endpoint for programmatic access or JS fetch."""
    return jsonify(stats_cache)


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    """Manual refresh trigger. Accepts optional start_date / end_date in body or query."""
    data = request.get_json(silent=True) or {}
    start_date = data.get("start_date") or request.args.get("start_date")
    end_date = data.get("end_date") or request.args.get("end_date")
    compute_stats(start_date=start_date, end_date=end_date)
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
