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
import sqlite3
from datetime import datetime, timedelta, timezone

import pytz
import requests
from flask import Flask, render_template, jsonify, request
from apscheduler.schedulers.background import BackgroundScheduler

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
eastern = pytz.timezone("US/Eastern")
DECAGON_API_KEY = os.environ.get("DECAGON_API_KEY", "")
DECAGON_API_BASE = os.environ.get("DECAGON_API_BASE", "https://api.decagon.ai")
PORT = int(os.environ.get("PORT", 5000))
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DB_PATH = os.environ.get("DB_PATH", "data/stats.db")

def init_db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS daily_stats (
                date TEXT PRIMARY KEY,
                total_conversations INTEGER,
                deflected INTEGER,
                escalated INTEGER,
                deflection_rate REAL,
                csat_average REAL,
                updated_at TIMESTAMP
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS daily_category_stats (
                date TEXT,
                category TEXT,
                subcategory TEXT,
                total_conversations INTEGER,
                deflected INTEGER,
                escalated INTEGER,
                PRIMARY KEY (date, category, subcategory)
            )
        ''')
        conn.commit()

init_db()

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
    "day_labels": [],
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
    "hourly_volume": {},
    "hourly_by_day": {},
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

    # We'll calculate days based on US/Eastern time
    now_utc = datetime.now(timezone.utc)
    now_est = now_utc.astimezone(eastern)

    if start_date:
        start_dt_est = eastern.localize(datetime.strptime(start_date, "%Y-%m-%d"))
    else:
        start_dt_est = (now_est - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)

    if end_date:
        end_dt_est = eastern.localize(datetime.strptime(end_date, "%Y-%m-%d")).replace(
            hour=23, minute=59, second=59, microsecond=999999
        )
    else:
        end_dt_est = now_est

    min_ts = start_dt_est.timestamp()
    max_ts = end_dt_est.timestamp()

    logger.info(
        f"Refreshing stats for {start_dt_est.strftime('%Y-%m-%d')} "
        f"→ {end_dt_est.strftime('%Y-%m-%d')} EST"
    )

    # -- 1. Pull conversations -------------------------------------------------
    conversations = export_all_conversations(min_ts, max_ts)

    total = len(conversations)
    deflected = 0
    escalated = 0
    category_counts = {}

    csat_values = []
    csat_distribution = {}

    cat_detail_map = {}
    error_items = []

    # Hourly volume: key = "HH:00", value = {"total": int, "deflected": int}
    hourly_volume = {}
    # Per-day hourly: {"YYYY-MM-DD": {"HH:00": {"total": int, "deflected": int}}}
    hourly_by_day = {}
    
    # Per-day CSAT tracking: {"YYYY-MM-DD": [csat1, csat2, ...]}
    csat_by_day = {}

    # Daily per-category: daily_cat_map[day_str][(parent, sub)] = {total, deflected}
    daily_cat_map = {}

    for convo in conversations:
        # ---- Deflection / Escalation ----
        is_undeflected = convo.get("undeflected", False)
        destination = convo.get("destination", "")
        is_escalated = is_undeflected or destination == "AGENT"

        if is_escalated:
            escalated += 1
        else:
            deflected += 1

        # ---- Hourly volume ----
        created_at_str = convo.get("created_at", "")
        try:
            created_dt_utc = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
            created_dt_est = created_dt_utc.astimezone(eastern)
            hour_key = created_dt_est.strftime("%H:00")
            day_key  = created_dt_est.strftime("%Y-%m-%d")
            # Aggregate
            if hour_key not in hourly_volume:
                hourly_volume[hour_key] = {"total": 0, "deflected": 0}
            hourly_volume[hour_key]["total"] += 1
            if not is_escalated:
                hourly_volume[hour_key]["deflected"] += 1
            # Per-day
            if day_key not in hourly_by_day:
                hourly_by_day[day_key] = {}
            if hour_key not in hourly_by_day[day_key]:
                hourly_by_day[day_key][hour_key] = {"total": 0, "deflected": 0}
            hourly_by_day[day_key][hour_key]["total"] += 1
            if not is_escalated:
                hourly_by_day[day_key][hour_key]["deflected"] += 1
        except (ValueError, AttributeError):
            pass

        # ---- Categories from Insight tags (all_tags hierarchy) ----
        # all_tags is a dict of {hierarchy_id: {name: str, tags: [...]}}
        # Skip intent hierarchies where top-level tags start with "I " (e.g. "I need...")
        parent_cat = None
        subcategories = []

        all_tags = convo.get("all_tags", {}) or {}

        def _looks_like_intent(h):
            top = [t.get("name", "") for t in (h.get("tags") or []) if t.get("level", 0) == 0]
            if not top:
                return False
            intent = sum(1 for n in top if n.strip().lower().startswith("i "))
            return (intent / len(top)) > 0.4

        insight_hierarchy = None
        fallback_hierarchy = None
        for hierarchy_id, hierarchy in all_tags.items():
            if _looks_like_intent(hierarchy):
                continue  # skip intent hierarchies
            h_name = (hierarchy.get("name") or "").lower()
            if "insight" in h_name:
                insight_hierarchy = hierarchy
                break
            if fallback_hierarchy is None:
                fallback_hierarchy = hierarchy

        chosen_hierarchy = insight_hierarchy or fallback_hierarchy

        if chosen_hierarchy:
            for tag in (chosen_hierarchy.get("tags") or []):
                level = tag.get("level", 0)
                name = tag.get("name", "Uncategorized")
                if level == 0:
                    parent_cat = name
                else:
                    subcategories.append(name)

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

        # Daily per-category tracking
        created_at_str2 = convo.get("created_at", "")
        try:
            created_dt_utc2 = datetime.fromisoformat(created_at_str2.replace("Z", "+00:00"))
            created_dt_est2 = created_dt_utc2.astimezone(eastern)
            day_str = created_dt_est2.strftime("%Y-%m-%d")
            if day_str not in daily_cat_map:
                daily_cat_map[day_str] = {}
            for ckey in [(parent_cat, None)] + [(parent_cat, s) for s in subcategories]:
                if ckey not in daily_cat_map[day_str]:
                    daily_cat_map[day_str][ckey] = {"total": 0, "deflected": 0}
                daily_cat_map[day_str][ckey]["total"] += 1
                if not is_escalated:
                    daily_cat_map[day_str][ckey]["deflected"] += 1
        except (ValueError, AttributeError):
            pass

        # ---- CSAT (embedded in conversation object) ----
        csat_score = convo.get("csat")
        if csat_score is not None:
            try:
                csat_score = int(csat_score)
                csat_values.append(csat_score)
                key = str(csat_score)
                csat_distribution[key] = csat_distribution.get(key, 0) + 1
                
                # Global day tracking for CSAT
                created_dt_utc = datetime.fromisoformat(convo.get("created_at", "").replace("Z", "+00:00"))
                day_key_csat = created_dt_utc.astimezone(eastern).strftime("%Y-%m-%d")
                csat_by_day.setdefault(day_key_csat, []).append(csat_score)
            except (ValueError, TypeError, AttributeError):
                pass

        # ---- Watchtower / Auto QA Reviews ----
        watchtower = convo.get("watchtower_reviews", [])
        for review in watchtower:
            result = review.get("result") or ""
            # Track all reviews, but flag failures/issues prominently
            rubric_score = review.get("rubric_score")
            rubric_review = review.get("rubric_review") or {}

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

    # -- Upsert daily_stats to SQLite --
    try:
        with sqlite3.connect(DB_PATH) as conn:
            for day, h_vols in hourly_by_day.items():
                day_tot = sum(h["total"] for h in h_vols.values())
                day_defl = sum(h["deflected"] for h in h_vols.values())
                day_esc = day_tot - day_defl
                day_rate = round((day_defl / day_tot) * 100, 1) if day_tot > 0 else 0.0
                
                day_csats = csat_by_day.get(day, [])
                day_csat_avg = round(sum(day_csats) / len(day_csats), 2) if day_csats else None
                
                conn.execute('''
                    INSERT INTO daily_stats (date, total_conversations, deflected, escalated, deflection_rate, csat_average, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(date) DO UPDATE SET
                        total_conversations=excluded.total_conversations,
                        deflected=excluded.deflected,
                        escalated=excluded.escalated,
                        deflection_rate=excluded.deflection_rate,
                        csat_average=excluded.csat_average,
                        updated_at=excluded.updated_at
                ''', (day, day_tot, day_defl, day_esc, day_rate, day_csat_avg, now_utc.isoformat()))
            
            # Upsert daily category stats
            for c_date, cat_data in daily_cat_map.items():
                for (cat_parent, cat_sub), stats in cat_data.items():
                    c_tot = stats["total"]
                    c_def = stats["deflected"]
                    c_esc = c_tot - c_def
                    cat_sub_str = cat_sub if cat_sub else ""
                    conn.execute('''
                        INSERT INTO daily_category_stats (date, category, subcategory, total_conversations, deflected, escalated)
                        VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(date, category, subcategory) DO UPDATE SET
                            total_conversations=excluded.total_conversations,
                            deflected=excluded.deflected,
                            escalated=excluded.escalated
                    ''', (c_date, cat_parent, cat_sub_str, c_tot, c_def, c_esc))
            conn.commit()
    except Exception as e:
        logger.error(f"Failed to upsert daily stats to SQLite: {e}")

    # Build daily_category_stats: sorted day labels + per-category daily rates
    day_labels_db = sorted(daily_cat_map.keys())
    day_labels = [datetime.strptime(d, "%Y-%m-%d").strftime("%m/%d") for d in day_labels_db]
    # Attach daily deflection rates to each category_detail row
    for row in category_detail:
        ckey = (row["category"], row["subcategory"])
        day_rates = []
        for d in day_labels_db:
            ddata = daily_cat_map.get(d, {}).get(ckey)
            if ddata and ddata["total"] > 0:
                day_rates.append(round((ddata["deflected"] / ddata["total"]) * 100, 1))
            else:
                day_rates.append(None)
        row["day_rates"] = day_rates
        row["day_totals"] = [
            (daily_cat_map.get(d, {}).get(ckey) or {}).get("total", 0)
            for d in day_labels_db
        ]

    # -- 2. Update cache -------------------------------------------------------
    stats_cache = {
        "last_updated": now_utc.isoformat(),
        "date_range": f"{start_dt_est.strftime('%Y-%m-%d')} to {end_dt_est.strftime('%Y-%m-%d')}",
        "start_date": start_dt_est.strftime("%Y-%m-%d"),
        "end_date": end_dt_est.strftime("%Y-%m-%d"),
        "deflection_rate": deflection_rate,
        "categories": category_pcts,
        "day_labels": day_labels,
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
        "hourly_volume": dict(sorted(hourly_volume.items())),
        "hourly_by_day": {d: dict(sorted(v.items())) for d, v in sorted(hourly_by_day.items())},
        "error": None,
    }
    logger.info("Stats refreshed successfully")

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/")
def dashboard():
    return render_template("dashboard.html", stats=stats_cache)


@app.route("/categories")
def categories_page():
    return render_template("categories.html", stats=stats_cache)


@app.route("/history")
def history_page():
    history_data = []
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            # 1. Main daily stats
            rows = conn.execute("SELECT * FROM daily_stats ORDER BY date DESC").fetchall()
            daily_rows = [dict(row) for row in rows]
            
            # 2. Per-category history, grouped by date
            cat_rows = conn.execute("""
                SELECT date, category, subcategory, total_conversations, deflected
                FROM daily_category_stats 
                ORDER BY date DESC, total_conversations DESC
            """).fetchall()
            
            # Organize categories by date
            cat_by_date = {}
            for cr in cat_rows:
                d = cr["date"]
                if d not in cat_by_date:
                    cat_by_date[d] = []
                cat_by_date[d].append(dict(cr))
            
            # 3. Merge category data into main history rows
            for row in daily_rows:
                row["categories"] = cat_by_date.get(row["date"], [])
            
            history_data = daily_rows
    except Exception as e:
        logger.error(f"Failed to fetch history: {e}")
    return render_template("history.html", stats=stats_cache, history=history_data)


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
# Scheduler: refresh daily at Midnight EST
# ---------------------------------------------------------------------------
scheduler = BackgroundScheduler(timezone=eastern)
scheduler.add_job(compute_stats, "cron", hour=0, minute=0)
scheduler.start()


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------
# Run initial fetch on startup
logger.info("Running startup 24h refresh...")
compute_stats()

# One-time backfill for last calendar month (March 2026) as requested
try:
    logger.info("Running manual backfill for March 2026...")
    compute_stats(start_date="2026-03-01", end_date="2026-03-31")
    logger.info("Backfill for March 2026 complete.")
except Exception as e:
    logger.error(f"Backfill failed: {e}")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=False)
