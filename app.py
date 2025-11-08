#!/usr/bin/env python3
from flask import Flask, jsonify, request, render_template
from apscheduler.schedulers.background import BackgroundScheduler
import sqlite3, os, time, threading
from datetime import datetime, timedelta
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import logging

DB_PATH = 'gold_prices.db'
FETCH_INTERVAL_SECONDS = 2 * 60 * 60  # every 2 hours
SJC_URL = "https://sjc.com.vn/GoldPrice/Services/PriceService.ashx"
TARGET_BRANCH = "Hồ Chí Minh"

app = Flask(__name__, static_folder='static', template_folder='templates')
logging.basicConfig(level=logging.INFO)


# -------------------- DB init & migration --------------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='prices'")
    if not c.fetchone():
        c.execute('''
            CREATE TABLE IF NOT EXISTS prices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp INTEGER NOT NULL,
                buy REAL,
                sell REAL
            )
        ''')
        conn.commit()
        conn.close()
        app.logger.info('Created new prices table')
        return

    c.execute("PRAGMA table_info(prices)")
    cols = [r[1] for r in c.fetchall()]

    if 'buy' in cols and 'sell' in cols:
        conn.close()
        return

    if 'price' in cols:
        c.execute('''
            CREATE TABLE IF NOT EXISTS prices_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp INTEGER NOT NULL,
                buy REAL,
                sell REAL
            )
        ''')
        conn.commit()
        try:
            c.execute("INSERT INTO prices_new (id, timestamp, buy, sell) SELECT id, timestamp, NULL, price FROM prices")
        except sqlite3.OperationalError:
            rows = c.execute("SELECT id, timestamp, price FROM prices").fetchall()
            for r in rows:
                _id, ts, price = r
                c.execute("INSERT INTO prices_new (id, timestamp, buy, sell) VALUES (?, ?, ?, ?)",
                          (_id, ts, None, price))
        conn.commit()
        c.execute("DROP TABLE prices")
        c.execute("ALTER TABLE prices_new RENAME TO prices")
        conn.commit()
        conn.close()
        app.logger.info('Migration complete.')
        return

    if 'buy' not in cols:
        c.execute("ALTER TABLE prices ADD COLUMN buy REAL")
    if 'sell' not in cols:
        c.execute("ALTER TABLE prices ADD COLUMN sell REAL")
    conn.commit()
    conn.close()
    app.logger.info('Added missing columns')


def get_db_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def insert_price(ts_unix, buy, sell):
    db = sqlite3.connect(DB_PATH)
    c = db.cursor()
    c.execute('INSERT INTO prices(timestamp, buy, sell) VALUES(?, ?, ?)',
              (int(ts_unix), float(buy) if buy is not None else None, float(sell) if sell is not None else None))
    db.commit()
    db.close()


# -------------------- SJC API helpers --------------------
def call_sjc_for_date(date_str):
    payload = {"method": "GetSJCGoldPriceByDate", "toDate": date_str}
    headers = {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    }
    session = requests.Session()
    retries = Retry(total=4, backoff_factor=0.5,
                    status_forcelist=(429, 500, 502, 503, 504),
                    allowed_methods=frozenset(["POST", "GET"]))
    adapter = HTTPAdapter(max_retries=retries)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    try:
        r = session.post(SJC_URL, data=payload, headers=headers, timeout=15, verify=True)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        app.logger.warning('SJC API error for date %s: %s', date_str, e)
        return None


def extract_buy_sell_from_response(j):
    try:
        if not j or not isinstance(j, dict) or not j.get('success'):
            return None, None
        for entry in j.get('data', []):
            if entry.get('BranchName') == TARGET_BRANCH:
                buy_val = entry.get('BuyValue')
                sell_val = entry.get('SellValue')
                if buy_val is None:
                    b = entry.get('Buy')
                    if isinstance(b, str) and b.strip() != '':
                        buy_val = float(b.replace(',', ''))
                if sell_val is None:
                    s = entry.get('Sell')
                    if isinstance(s, str) and s.strip() != '':
                        sell_val = float(s.replace(',', ''))
                return buy_val, sell_val
        return None, None
    except Exception as e:
        app.logger.warning('Error extracting buy/sell: %s', e)
        return None, None


def fetch_price_for_date(date_obj):
    date_str = date_obj.strftime('%d/%m/%Y')
    j = call_sjc_for_date(date_str)
    if not j:
        return None
    buy, sell = extract_buy_sell_from_response(j)
    if buy is None and sell is None:
        return None
    ts = int(time.mktime(date_obj.replace(hour=12, minute=0, second=0).timetuple()))
    return ts, buy, sell


def fetch_latest_price():
    date_obj = datetime.now()
    res = fetch_price_for_date(date_obj)
    if not res:
        return None
    ts_day, buy, sell = res
    return int(time.time()), buy, sell


# -------------------- queries for modes --------------------
def get_today_records():
    db = get_db_conn()
    c = db.cursor()
    q = """
    SELECT timestamp, buy, sell
    FROM prices
    WHERE date(timestamp, 'unixepoch', 'localtime') = date('now','localtime')
    ORDER BY timestamp ASC
    """
    c.execute(q)
    rows = c.fetchall()
    db.close()
    return [{'timestamp': r['timestamp'], 'buy': r['buy'], 'sell': r['sell']} for r in rows]


def get_daily_latest(days):
    db = get_db_conn()
    c = db.cursor()
    q = """
    SELECT p.timestamp, p.buy, p.sell
    FROM prices p
    JOIN (
      SELECT date(timestamp, 'unixepoch', 'localtime') as d, MAX(timestamp) as maxts
      FROM prices
      GROUP BY d
    ) m ON p.timestamp = m.maxts
    ORDER BY p.timestamp DESC
    LIMIT ?
    """
    c.execute(q, (days,))
    rows = c.fetchall()
    db.close()
    rows = list(reversed(rows))
    return [{'timestamp': r['timestamp'], 'buy': r['buy'], 'sell': r['sell']} for r in rows]


# -------------------- Scheduler job --------------------
def job_fetch_and_store():
    result = fetch_latest_price()
    if result is None:
        app.logger.info('Fetch returned no result; skipping insert')
        return
    ts, buy, sell = result
    insert_price(ts, buy if buy is not None else 0.0, sell if sell is not None else 0.0)
    app.logger.info('Stored gold price: buy=%s sell=%s at %s', buy, sell, datetime.fromtimestamp(ts))


# -------------------- Web endpoints --------------------
@app.route('/api/prices')
def api_prices():
    mode = request.args.get('mode', '7d')  # 'today', '7d', '30d'
    if mode == 'today':
        data = get_today_records()
    elif mode == '30d':
        limit = request.args.get('limit', default=30, type=int)
        data = get_daily_latest(limit)
    else:
        limit = request.args.get('limit', default=7, type=int)
        data = get_daily_latest(limit)
    return jsonify({'status': 'ok', 'data': data, 'last_update': data[-1]['timestamp'] if data else None})


@app.route('/api/fetch-history', methods=['POST'])
def api_fetch_history():
    try:
        days = int(request.args.get('days', 7))
    except:
        days = 7
    inserted = 0
    for d in range(days):
        date_obj = datetime.now() - timedelta(days=d)
        res = fetch_price_for_date(date_obj)
        if res:
            ts, buy, sell = res
            db = sqlite3.connect(DB_PATH)
            c = db.cursor()
            c.execute('SELECT COUNT(1) FROM prices WHERE timestamp = ?', (ts,))
            exists = c.fetchone()[0]
            db.close()
            if not exists:
                insert_price(ts, buy if buy is not None else 0.0, sell if sell is not None else 0.0)
                inserted += 1
    return jsonify({'status': 'ok', 'inserted': inserted})


@app.route('/')
def index():
    return render_template('index.html')


# -------------------- App startup --------------------
if __name__ == '__main__':
    init_db()

    if not os.path.exists(DB_PATH) or os.path.getsize(DB_PATH) == 0:
        first = fetch_latest_price()
        if first:
            ts, buy, sell = first
            insert_price(ts, buy if buy is not None else 0.0, sell if sell is not None else 0.0)

    scheduler = BackgroundScheduler()
    scheduler.add_job(job_fetch_and_store, 'interval', seconds=FETCH_INTERVAL_SECONDS, next_run_time=None)
    scheduler.start()

    threading.Thread(target=job_fetch_and_store, daemon=True).start()

    port = int(os.getenv('PORT', 3000))
    app.run(host='0.0.0.0', port=port)
