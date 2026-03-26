#!/usr/bin/env python3
from flask import Flask, jsonify, request, render_template
from apscheduler.schedulers.background import BackgroundScheduler
import sqlite3, os, time, threading
from datetime import datetime, timedelta, timezone

VN_TZ = timezone(timedelta(hours=7))  # UTC+7, Mi Hồng returns dates in this tz
VN_OFFSET_SECS = 7 * 3600  # 25200 – used in SQLite date() to group by VN date
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import logging
import argparse


DB_PATH = 'gold_prices.db'
FETCH_INTERVAL_SECONDS = 60 * 60  # every 1 hour
MIHONG_BASE = "https://api.mihong.vn/v1/gold-prices"
SUPPORTED_TYPES = ("SJC", "999")

app = Flask(__name__, static_folder='static', template_folder='templates')
logging.basicConfig(level=logging.INFO)


# -------------------- DB init & migration --------------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS prices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp INTEGER NOT NULL,
            gold_type TEXT NOT NULL DEFAULT 'SJC',
            buy REAL,
            sell REAL
        )
    ''')
    conn.commit()

    # Migration: add gold_type column if missing
    c.execute("PRAGMA table_info(prices)")
    cols = [r[1] for r in c.fetchall()]
    if 'gold_type' not in cols:
        c.execute("ALTER TABLE prices ADD COLUMN gold_type TEXT NOT NULL DEFAULT 'SJC'")
        conn.commit()
        app.logger.info('Migration: added gold_type column')

    conn.close()
    app.logger.info('DB initialized')


def get_db_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def insert_price(ts_unix, gold_type, buy, sell):
    db = sqlite3.connect(DB_PATH)
    c = db.cursor()
    c.execute('INSERT INTO prices(timestamp, gold_type, buy, sell) VALUES(?, ?, ?, ?)',
              (int(ts_unix), gold_type,
               float(buy) if buy is not None else None,
               float(sell) if sell is not None else None))
    db.commit()
    db.close()


def upsert_daily_price(ts_unix, gold_type, buy, sell):
    """
    For a given day (using midday timestamp), insert only if no record exists for that day+type.
    Used during backfill to avoid duplicates.
    """
    db = sqlite3.connect(DB_PATH)
    c = db.cursor()
    c.execute(
        "SELECT COUNT(1) FROM prices WHERE gold_type = ? AND date(timestamp + ?, 'unixepoch') = date(? + ?, 'unixepoch')",
        (gold_type, VN_OFFSET_SECS, int(ts_unix), VN_OFFSET_SECS)
    )
    if c.fetchone()[0] == 0:
        c.execute('INSERT INTO prices(timestamp, gold_type, buy, sell) VALUES(?, ?, ?, ?)',
                  (int(ts_unix), gold_type,
                   float(buy) if buy is not None else None,
                   float(sell) if sell is not None else None))
        db.commit()
        db.close()
        return True
    db.close()
    return False


# -------------------- Mi Hồng API helpers --------------------
def _make_session():
    session = requests.Session()
    retries = Retry(total=4, backoff_factor=0.5,
                    status_forcelist=(429, 500, 502, 503, 504),
                    allowed_methods=frozenset(["GET"]))
    adapter = HTTPAdapter(max_retries=retries)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def fetch_mihong_current():
    """Fetch all current gold prices from Mi Hồng (no date filter)."""
    try:
        r = _make_session().get(MIHONG_BASE, params={"market": "domestic"}, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        app.logger.warning('Mi Hồng current API error: %s', e)
        return None


def fetch_mihong_history(gold_type: str, last: str):
    """
    Fetch price history for a specific gold type.
    `last` can be e.g. '15d' (daily history) or '24h' (intraday).
    Returns list of {timestamp, buy, sell} dicts sorted ASC.
    """
    try:
        r = _make_session().get(
            MIHONG_BASE,
            params={"market": "domestic", "goldCode": gold_type, "last": last},
            timeout=15
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        app.logger.warning('Mi Hồng history API error (%s, %s): %s', gold_type, last, e)
        return []

    result = []
    for entry in (data or []):
        dt_str = entry.get("dateTime", "")
        buy = entry.get("buyingPrice")
        sell = entry.get("sellingPrice")
        try:
            # dateTime from Mi Hồng is UTC+7 — attach timezone before converting
            dt = datetime.strptime(dt_str, "%d/%m/%Y %H:%M").replace(tzinfo=VN_TZ)
            ts = int(dt.timestamp())
        except Exception:
            continue
        result.append({
            "timestamp": ts,
            "buy": float(buy) if buy else None,
            "sell": float(sell) if sell else None,
        })

    result.sort(key=lambda x: x["timestamp"])
    return result


# -------------------- Scheduler job --------------------
def job_fetch_and_store():
    """Fetch current prices every hour and store in DB."""
    data = fetch_mihong_current()
    if not data:
        app.logger.info('Fetch returned no results; skipping insert')
        return
    ts = int(time.time())
    for entry in data:
        code = entry.get("code", "").upper()
        if code not in SUPPORTED_TYPES:
            continue
        buy = entry.get("buyingPrice")
        sell = entry.get("sellingPrice")
        insert_price(ts, code,
                     float(buy) if buy else 0.0,
                     float(sell) if sell else 0.0)
        app.logger.info('Stored %s: buy=%s sell=%s at %s', code, buy, sell, datetime.fromtimestamp(ts))


def backfill_from_history(days: int = 15) -> int:
    """
    Backfill DB using Mi H\u1ed3ng history APIs:
    - ?last=<days>d  : one record per day for the last N days
    - ?last=24h      : intraday ticks for the last 24h (seeds today's data)
    Only inserts records that don't already exist (by exact timestamp + gold_type).
    Returns total number of records inserted.
    """
    inserted = 0
    for gold_type in SUPPORTED_TYPES:
        # --- daily history (last N days) ---
        for record in fetch_mihong_history(gold_type, f"{days}d"):
            ok = upsert_daily_price(record['timestamp'], gold_type, record['buy'], record['sell'])
            if ok:
                inserted += 1
                app.logger.info('Backfilled daily %s @ %s', gold_type,
                                datetime.fromtimestamp(record['timestamp']).strftime('%Y-%m-%d'))

        # --- intraday history (last 24h, seeds today's ticks) ---
        today_vn = datetime.now(VN_TZ).date()
        for record in fetch_mihong_history(gold_type, "24h"):
            # only insert records that belong to today (UTC+7)
            if datetime.fromtimestamp(record['timestamp'], tz=VN_TZ).date() != today_vn:
                continue
            db = sqlite3.connect(DB_PATH)
            c = db.cursor()
            c.execute('SELECT COUNT(1) FROM prices WHERE timestamp = ? AND gold_type = ?',
                      (record['timestamp'], gold_type))
            exists = c.fetchone()[0]
            db.close()
            if not exists:
                insert_price(record['timestamp'], gold_type,
                             record['buy'] or 0.0, record['sell'] or 0.0)
                inserted += 1
                app.logger.info('Backfilled intraday %s @ %s', gold_type,
                                datetime.fromtimestamp(record['timestamp'], tz=VN_TZ).strftime('%H:%M'))
    return inserted


# -------------------- DB queries --------------------
def get_today_records(gold_type='SJC'):
    """Read intraday records for today from DB."""
    db = get_db_conn()
    c = db.cursor()
    c.execute("""
        SELECT timestamp, buy, sell FROM prices
        WHERE gold_type = ?
          AND date(timestamp + ?, 'unixepoch') = date(strftime('%s','now') + ?, 'unixepoch')
        ORDER BY timestamp ASC
    """, (gold_type, VN_OFFSET_SECS, VN_OFFSET_SECS))
    rows = c.fetchall()
    db.close()
    return [{'timestamp': r['timestamp'], 'buy': r['buy'], 'sell': r['sell']} for r in rows]


def get_daily_latest(days, gold_type='SJC'):
    """Return one (latest) record per day for the last `days` days."""
    db = get_db_conn()
    c = db.cursor()
    c.execute("""
        SELECT p.timestamp, p.buy, p.sell
        FROM prices p
        JOIN (
          SELECT date(timestamp + ?, 'unixepoch') as d, MAX(timestamp) as maxts
          FROM prices
          WHERE gold_type = ?
          GROUP BY d
        ) m ON p.timestamp = m.maxts AND p.gold_type = ?
        ORDER BY p.timestamp DESC
        LIMIT ?
    """, (VN_OFFSET_SECS, gold_type, gold_type, days))
    rows = c.fetchall()
    db.close()
    return [{'timestamp': r['timestamp'], 'buy': r['buy'], 'sell': r['sell']} for r in reversed(rows)]


# -------------------- Web endpoints --------------------
@app.route('/api/prices')
def api_prices():
    mode = request.args.get('mode', '7d')
    gold_type = request.args.get('type', 'SJC').upper()
    if gold_type not in SUPPORTED_TYPES:
        gold_type = 'SJC'

    if mode == 'today':
        data = get_today_records(gold_type)
    else:
        limit = request.args.get('limit', default=None, type=int)
        if limit is None:
            limit = 30 if mode == '30d' else 7
        data = get_daily_latest(limit, gold_type)

    return jsonify({'status': 'ok', 'data': data, 'last_update': data[-1]['timestamp'] if data else None})


@app.route('/api/fetch-history', methods=['POST'])
def api_fetch_history():
    try:
        days = int(request.args.get('days', 15))
    except Exception:
        days = 15
    inserted = backfill_from_history(days)
    return jsonify({'status': 'ok', 'inserted': inserted})


@app.route('/')
def index():
    return render_template('index.html')


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Gold price service")
    parser.add_argument('--port', type=int, default=int(os.getenv('PORT', 3000)),
                        help='Port to run the Flask app (default from PORT env or 3000)')
    parser.add_argument('--backfill', type=int, default=15,
                        help='Days to backfill on startup using Mi Hong history API (default: 15)')
    args = parser.parse_args()

    # Initialize DB
    init_db()

    # Backfill history on startup
    app.logger.info("Backfilling last %d days from Mi Hồng history API...", args.backfill)
    inserted = backfill_from_history(args.backfill)
    app.logger.info("Backfill complete: %d records inserted", inserted)

    # Fetch current price immediately
    app.logger.info("Fetching current prices...")
    job_fetch_and_store()

    # Start hourly scheduler
    scheduler = BackgroundScheduler()
    scheduler.add_job(job_fetch_and_store, 'interval', seconds=FETCH_INTERVAL_SECONDS)
    scheduler.start()
    app.logger.info("Scheduler started (every %d seconds)", FETCH_INTERVAL_SECONDS)

    # Run Flask app
    app.run(host='0.0.0.0', port=args.port, debug=False)
