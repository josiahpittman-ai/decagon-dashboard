"""
Decagon Stats Dashboard
-----------------------
A Flask app that pulls daily deflection rate, category percentages, and CSAT
scores from Decagon's REST API and displays them on a clean dashboard.

Deploy to Render as a Web Service.
"""

import os
import json
import logging
import sqlite3
import threading
import traceback
import uuid
from datetime import datetime, timedelta, timezone
from collections import defaultdict

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
        # Persistent task store to fix multi-worker/process "not found" issues
        conn.execute('''
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                status TEXT,
                result TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.commit()

init_db()

# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------
stats_cache = {
    "last_updated": None, "start_date": None, "end_date": None,
    "date_range": "Initializing data...", "deflection_rate": 0,
    "categories": {}, "day_labels": [], "category_detail": [],
    "error_analysis": [], "csat": {"average": 0, "total_ratings": 0, "distribution": {}},
    "conversation_totals": {"total": 0, "deflected": 0, "escalated": 0},
    "hourly_volume": {}, "hourly_by_day": {}, "error": None,
}

# ---------------------------------------------------------------------------
# Decagon API helpers
# ---------------------------------------------------------------------------
def _headers():
    return {
        "Authorization": f"Bearer {DECAGON_API_KEY}",
        "Content-Type": "application/json",
    }

def stream_conversations(min_ts: float, max_ts: float):
    url = f"{DECAGON_API_BASE}/conversation/export"
    cursor = None
    while True:
        params = {
            "timestamp_filter": "created_at",
            "min_timestamp": min_ts, "max_timestamp": max_ts, "page_size": 1000,
        }
        if cursor: params["cursor"] = cursor
        try:
            resp = requests.get(url, headers=_headers(), params=params, timeout=60)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.error(f"Failed to export conversations: {e}")
            break
        conversations = data.get("conversations", [])
        for convo in conversations:
            yield convo
        cursor = data.get("next_cursor")
        if not cursor or not conversations:
            break

# ---------------------------------------------------------------------------
# Stats computation
# ---------------------------------------------------------------------------
def compute_stats(start_date: str = None, end_date: str = None, task_id: str = None):
    """
    Fetch data from Decagon and recompute all dashboard metrics.
    Syncs with DB tasks so multiple workers can see the progress.
    """
    global stats_cache

    local_stats = {
        "last_updated": None,
        "start_date": start_date or (datetime.now(eastern) - timedelta(days=7)).strftime("%Y-%m-%d"),
        "end_date": end_date or datetime.now(eastern).strftime("%Y-%m-%d"),
        "date_range": "Refreshing...",
        "deflection_rate": 0, "categories": {}, "day_labels": [], "category_detail": [],
        "error_analysis": [], "csat": {"average": 0, "total_ratings": 0, "distribution": {}},
        "conversation_totals": {"total": 0, "deflected": 0, "escalated": 0},
        "hourly_volume": {}, "hourly_by_day": {}, "error": None,
    }

    try:
        if not DECAGON_API_KEY:
            local_stats["error"] = "API Key not set."
            if task_id:
                with sqlite3.connect(DB_PATH) as conn:
                    conn.execute("UPDATE tasks SET status='complete', result=? WHERE id=?", (json.dumps(local_stats), task_id))
            return local_stats

        now_utc = datetime.now(timezone.utc)
        now_est = now_utc.astimezone(eastern)

        if start_date: start_dt = eastern.localize(datetime.strptime(start_date, "%Y-%m-%d"))
        else: start_dt = (now_est - timedelta(days=7)).replace(hour=0, minute=0, second=0, microsecond=0)

        if end_date: end_dt = eastern.localize(datetime.strptime(end_date, "%Y-%m-%d")).replace(hour=23, minute=59, second=59)
        else: end_dt = now_est

        min_ts, max_ts = start_dt.timestamp(), end_dt.timestamp()
        logger.info(f"Computing stats for {start_dt.date()} to {end_dt.date()} (task_id: {task_id})")

        total, deflected, escalated = 0, 0, 0
        hourly_volume = defaultdict(int)
        hourly_by_day = defaultdict(lambda: defaultdict(int))
        category_counts = defaultdict(int)
        cat_detail_map = defaultdict(lambda: {"total": 0, "deflected": 0, "escalated": 0})
        daily_cat_map = defaultdict(lambda: defaultdict(lambda: {"total": 0, "deflected": 0}))
        csat_values, error_items = [], []
        csat_distribution = {str(i): 0 for i in range(1, 6)}

        # Stream and aggregate
        for convo in stream_conversations(min_ts, max_ts):
            total += 1
            is_defl = not (convo.get("undeflected") or convo.get("destination") == "AGENT")
            if is_defl: deflected += 1
            else: escalated += 1

            c_ts = convo.get("created_at")
            if c_ts:
                dt_est = datetime.fromisoformat(c_ts.replace("Z", "+00:00")).astimezone(eastern)
                day, hour = dt_est.strftime("%Y-%m-%d"), dt_est.strftime("%H:00")
                hourly_volume[hour] += 1
                hourly_by_day[day][hour] += 1
                cv = convo.get("csat")
                if cv and str(cv) in csat_distribution:
                    cv = int(cv)
                    csat_values.append(cv)
                    csat_distribution[str(cv)] += 1

            # Insights
            parent_cat, subcats = "Uncategorized", []
            all_tags = convo.get("all_tags", {}) or {}
            for h in all_tags.values():
                if "insight" in (h.get("name") or "").lower():
                    for t in (h.get("tags") or []):
                        if t.get("level") == 0: parent_cat = t.get("name")
                        else: subcats.append(t.get("name"))
                    break
            category_counts[parent_cat] += 1
            for ckey in [(parent_cat, None)] + [(parent_cat, s) for s in subcats]:
                cat_detail_map[ckey]["total"] += 1
                if is_defl: cat_detail_map[ckey]["deflected"] += 1
                else: cat_detail_map[ckey]["escalated"] += 1
                if c_ts:
                    d_key = datetime.fromisoformat(c_ts.replace("Z", "+00:00")).astimezone(eastern).strftime("%Y-%m-%d")
                    daily_cat_map[d_key][ckey]["total"] += 1
                    if is_defl: daily_cat_map[d_key][ckey]["deflected"] += 1

            # Watchtower
            for r in convo.get("watchtower_reviews", []):
                if len(error_items) < 500:
                    error_items.append({"id":convo.get("id"), "job_name":r.get("job_name"), "result":r.get("result"), "rationale":r.get("rationale"), "rubric_score":r.get("rubric_score"), "category":parent_cat})

        # Calculate final
        category_pcts = {cat: round((val/total)*100, 1) for cat, val in category_counts.items()} if total > 0 else {}
        day_labels_db = sorted(hourly_by_day.keys())
        day_labels = [d[5:] for d in day_labels_db]
        
        category_detail = []
        parents = sorted(list({k[0] for k in cat_detail_map}), key=lambda p: cat_detail_map[(p, None)]["total"], reverse=True)
        for p in parents:
            p_data = cat_detail_map[(p, None)]
            category_detail.append({
                "category":p, "subcategory":None, "total":p_data["total"], "deflected":p_data["deflected"], "escalated":p_data["escalated"],
                "deflection_rate":round((p_data["deflected"]/p_data["total"])*100,1) if p_data["total"]>0 else 0,
                "percentage":round((p_data["total"]/total)*100, 1) if total>0 else 0,
                "day_rates":[round((daily_cat_map[d][(p,None)]["deflected"]/daily_cat_map[d][(p,None)]["total"])*100,1) if daily_cat_map[d][(p,None)]["total"]>0 else None for d in day_labels_db]
            })
            for s_key in sorted([k for k in cat_detail_map if k[0]==p and k[1]], key=lambda k:cat_detail_map[k]["total"], reverse=True):
                s_data = cat_detail_map[s_key]
                category_detail.append({
                    "category":p, "subcategory":s_key[1], "total":s_data["total"], "deflected":s_data["deflected"], "escalated":s_data["escalated"],
                    "deflection_rate":round((s_data["deflected"]/s_data["total"])*100,1) if s_data["total"]>0 else 0,
                    "percentage":round((s_data["total"]/total)*100, 1) if total>0 else 0,
                    "day_rates":[round((daily_cat_map[d][s_key]["deflected"]/daily_cat_map[d][s_key]["total"])*100,1) if daily_cat_map[d][s_key]["total"]>0 else None for d in day_labels_db]
                })

        local_results = {
            "last_updated": now_est.isoformat(),
            "start_date": start_dt.strftime("%Y-%m-%d"), "end_date": end_dt.strftime("%Y-%m-%d"),
            "date_range": f"{start_dt.strftime('%Y-%m-%d')} to {end_dt.strftime('%Y-%m-%d')}",
            "deflection_rate": round((deflected/total)*100, 1) if total > 0 else 0,
            "categories": category_pcts, "day_labels": day_labels, "category_detail": category_detail, "error_analysis": error_items,
            "csat": {"average": round(sum(csat_values)/len(csat_values), 2) if csat_values else 0, "total_ratings": len(csat_values), "distribution": csat_distribution},
            "conversation_totals": {"total": total, "deflected": deflected, "escalated": escalated},
            "hourly_volume": dict(sorted(hourly_volume.items())), "hourly_by_day": {d: dict(v) for d, v in hourly_by_day.items()}, "error": None
        }

        if start_date is None and end_date is None:
            stats_cache = local_results
            with sqlite3.connect(DB_PATH) as conn:
                for d in day_labels_db:
                    d_tot = sum(hourly_by_day[d].values())
                    conn.execute("INSERT OR REPLACE INTO daily_stats (date, total_conversations, deflection_rate, updated_at) VALUES (?,?,?,?)", (d, d_tot, local_results["deflection_rate"], now_est.isoformat()))
                conn.commit()

        if task_id:
            with sqlite3.connect(DB_PATH) as conn:
                conn.execute("UPDATE tasks SET status='complete', result=? WHERE id=?", (json.dumps(local_results), task_id))
        return local_results

    except Exception as e:
        logger.error(f"Compute error: {e}", exc_info=True)
        local_stats["error"] = str(e)
        if task_id:
            with sqlite3.connect(DB_PATH) as conn:
                conn.execute("UPDATE tasks SET status='error', result=? WHERE id=?", (json.dumps(local_stats), task_id))
        return local_stats

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/")
def dashboard():
    try:
        tid = request.args.get('task_id')
        stats = stats_cache
        if tid:
            with sqlite3.connect(DB_PATH) as conn:
                r = conn.execute("SELECT result FROM tasks WHERE id=?", (tid,)).fetchone()
                if r and r[0]: stats = json.loads(r[0])
        return render_template("dashboard.html", stats=stats)
    except Exception:
        logger.error(f"Dashboard render error: {traceback.format_exc()}")
        return f"<h1>Internal Server Error</h1><pre>{traceback.format_exc()}</pre>", 500

@app.route("/categories")
def categories_page():
    try:
        tid = request.args.get('task_id')
        stats = stats_cache
        if tid:
            with sqlite3.connect(DB_PATH) as conn:
                r = conn.execute("SELECT result FROM tasks WHERE id=?", (tid,)).fetchone()
                if r and r[0]: stats = json.loads(r[0])
        return render_template("categories.html", stats=stats)
    except Exception:
        logger.error(f"Categories render error: {traceback.format_exc()}")
        return f"<h1>Internal Server Error</h1><pre>{traceback.format_exc()}</pre>", 500

@app.route("/history")
def history_page():
    try:
        history_groups = {}
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM daily_stats ORDER BY date DESC").fetchall()
            for r in rows:
                dt = datetime.strptime(r["date"], "%Y-%m-%d")
                month = dt.strftime("%B %Y")
                if month not in history_groups: history_groups[month] = []
                history_groups[month].append(dict(r))
        history_data = [(m, history_groups[m]) for m in sorted(history_groups.keys(), key=lambda m: datetime.strptime(m, "%B %Y"), reverse=True)]
        return render_template("history.html", stats=stats_cache, history_groups=history_data)
    except Exception:
        logger.error(f"History render error: {traceback.format_exc()}")
        return f"<h1>Internal Server Error</h1><pre>{traceback.format_exc()}</pre>", 500

@app.route("/api/compute_async", methods=["POST"])
def compute_async():
    start, end = request.args.get('start_date'), request.args.get('end_date')
    task_id = str(uuid.uuid4())
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("INSERT INTO tasks (id, status) VALUES (?, 'pending')", (task_id,))
        conn.commit()
    threading.Thread(target=compute_stats, args=(start, end, task_id)).start()
    return jsonify({"task_id": task_id})

@app.route("/api/task_status/<task_id>")
def task_status(task_id):
    with sqlite3.connect(DB_PATH) as conn:
        r = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not r: return jsonify({"status": "not_found"}), 404
        return jsonify({"status": r[0]})

@app.route("/api/refresh", methods=["POST"])
def refresh():
    threading.Thread(target=compute_stats).start()
    return jsonify({"status": "ok", "message": "Refresh started in background"})

# ---------------------------------------------------------------------------
# Scheduler & Startup
# ---------------------------------------------------------------------------
scheduler = BackgroundScheduler(timezone=eastern)
scheduler.add_job(compute_stats, "cron", hour=0, minute=0)
scheduler.start()

logger.info("Service starting, launching background initial fetch...")
threading.Thread(target=compute_stats).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
