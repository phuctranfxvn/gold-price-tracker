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
import bs4
from bs4 import BeautifulSoup
import logging
import argparse
import yfinance as yf
import configparser


DB_PATH = 'gold_prices.db'
FETCH_INTERVAL_SECONDS = 60 * 60  # every 1 hour
MIHONG_BASE = "https://api.mihong.vn/v1/gold-prices"
SUPPORTED_TYPES = ("SJC", "999")
SUPPORTED_TYPES_QUERY = ("SJC", "999", "WORLD")  # includes world gold for querying

PVOIL_URL = "https://www.pvoil.com.vn/api/oilprice/load-view"
OIL_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
}
# In-memory cache for current oil prices
_oil_cache = {'data': None, 'ts': 0}
OIL_CACHE_TTL = 30 * 60  # 30 minutes

GOLDAPI_BASE = "https://www.goldapi.io/api/XAU/USD"

# Load backend config
config = configparser.ConfigParser()
config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'server.conf')
config.read(config_path)

GOLDAPI_KEY = config.get('GOLDAPI', 'API_KEY', fallback='goldapi-1n1yposmn8dj9x3-io')
GOLDAPI_HEADERS = {"x-access-token": GOLDAPI_KEY, "Content-Type": "application/json"}
USD_TO_VND = 26000          # fixed exchange rate VND/USD
OZ_TO_CHI = 37.5 / 31.1034768 / 10  # 1 chỉ = 3.75g, 1 troy oz = 31.1g

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

    # Oil prices table: one record per (date, name)
    c.execute('''
        CREATE TABLE IF NOT EXISTS oil_prices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            name TEXT NOT NULL,
            price INTEGER,
            change TEXT,
            UNIQUE(date, name)
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


# -------------------- Oil price helpers --------------------
def _scrape_oil_from_html(html_text):
    """Parse PVOil HTML and return list of {name, price, change} dicts."""
    soup = BeautifulSoup(html_text, 'html.parser')
    rows = soup.find_all('tr')
    results = []
    for row in rows:
        cols = row.find_all('td')
        if len(cols) >= 4:
            raw_name = cols[1].get_text(strip=True)
            raw_price = cols[2].get_text(strip=True)
            raw_change = cols[3].get_text(strip=True)
            if 'đ' in raw_price:
                clean_price = raw_price.replace('đ', '').replace('.', '').strip()
                try:
                    results.append({
                        'name': raw_name,
                        'price': int(clean_price),
                        'change': raw_change
                    })
                except ValueError:
                    continue
    return results


def fetch_oil_prices(date_str=None):
    """
    Fetch oil prices from PVOil.
    date_str: None → current prices; 'DD/MM/YYYY' → prices for that date (at 22:00).
    Returns list of {name, price, change} or None on error.
    """
    params = {}
    if date_str:
        params['date'] = f"{date_str} 22:00:00"
    try:
        resp = requests.get(PVOIL_URL, headers=OIL_HEADERS, params=params, timeout=15)
        resp.raise_for_status()
        return _scrape_oil_from_html(resp.text)
    except Exception as e:
        app.logger.warning('PVOil fetch error (date=%s): %s', date_str, e)
        return None


def store_oil_prices(date_str, items):
    """Upsert oil price records for a given date (YYYY-MM-DD format)."""
    db = sqlite3.connect(DB_PATH)
    c = db.cursor()
    inserted = 0
    for item in items:
        try:
            c.execute(
                'INSERT OR IGNORE INTO oil_prices(date, name, price, change) VALUES(?, ?, ?, ?)',
                (date_str, item['name'], item['price'], item['change'])
            )
            if c.rowcount > 0:
                inserted += 1
        except Exception as e:
            app.logger.warning('oil_prices insert error: %s', e)
    db.commit()
    db.close()
    return inserted


def backfill_oil_prices(days=15):
    """Backfill oil prices for the last `days` days."""
    inserted_total = 0
    today = datetime.now(VN_TZ).date()
    for i in range(days):
        day = today - timedelta(days=i)
        date_str_api = day.strftime('%d/%m/%Y')   # DD/MM/YYYY for PVOil API
        date_str_db = day.strftime('%Y-%m-%d')    # YYYY-MM-DD for DB
        # Skip if already in DB
        db = sqlite3.connect(DB_PATH)
        c = db.cursor()
        c.execute('SELECT COUNT(1) FROM oil_prices WHERE date = ?', (date_str_db,))
        exists = c.fetchone()[0]
        db.close()
        if exists > 0:
            app.logger.info('Oil prices for %s already in DB, skipping', date_str_db)
            continue
        items = fetch_oil_prices(date_str_api)
        if items:
            n = store_oil_prices(date_str_db, items)
            inserted_total += n
            app.logger.info('Backfilled oil %s: %d records', date_str_db, n)
        else:
            app.logger.warning('No oil data for %s', date_str_db)
        time.sleep(0.5)  # polite delay
    return inserted_total


# -------------------- Scheduler job --------------------
def job_fetch_and_store():
    """Fetch current prices every hour and store in DB."""
    data = fetch_mihong_current()
    ts = int(time.time())
    
    if data:
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
    else:
        app.logger.info('Mi Hong fetch returned no results; skipping SJC/999 insert')

    # Fetch world gold
    try:
        world_price = fetch_world_gold()
        if world_price:
            insert_price(ts, 'WORLD', world_price, world_price)
            app.logger.info('Stored WORLD: %s VND/chi at %s', world_price, datetime.fromtimestamp(ts))
    except Exception as e:
        app.logger.warning('World gold fetch in job failed: %s', e)

    # Fetch oil prices
    try:
        today_db = datetime.now(VN_TZ).strftime('%Y-%m-%d')
        oil_items = fetch_oil_prices(None)  # fetch current
        if oil_items:
            n = store_oil_prices(today_db, oil_items)
            app.logger.info('Stored OIL prices for %s: %d records inserted/updated', today_db, n)
    except Exception as e:
        app.logger.warning('Oil fetch in job failed: %s', e)


def backfill_intraday_today() -> int:
    """
    Backfill DB using Mi H\u1ed3ng ?last=24h API to seed today's ticks.
    Returns number of records inserted.
    """
    inserted = 0
    today_vn = datetime.now(VN_TZ).date()
    for gold_type in SUPPORTED_TYPES:
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


def backfill_from_history(days: int = 15) -> int:
    """
    Backfill DB using Mi H\u1ed3ng history APIs:
    - ?last=<days>d: daily records
    - ?last=24h: intraday for today
    """
    inserted = 0
    for gold_type in SUPPORTED_TYPES:
        for record in fetch_mihong_history(gold_type, f"{days}d"):
            ok = upsert_daily_price(record['timestamp'], gold_type, record['buy'], record['sell'])
            if ok:
                inserted += 1
                app.logger.info('Backfilled daily %s @ %s', gold_type,
                                datetime.fromtimestamp(record['timestamp']).strftime('%Y-%m-%d'))
    # Also do today's intraday
    inserted += backfill_intraday_today()
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


# -------------------- World gold (GoldAPI) --------------------
def fetch_world_gold(date_str=None):
    """
    Fetch XAU/USD from GoldAPI.
    date_str: None → current price; 'YYYYMMDD' → historical close.
    Returns price in VND per luong, or None on error.
    """
    url = GOLDAPI_BASE
    if date_str:
        url = f"{GOLDAPI_BASE}/{date_str}"
    try:
        resp = requests.get(url, headers=GOLDAPI_HEADERS, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        price_usd = data.get('price') or data.get('close_price')
        if price_usd:
            return round(float(price_usd) * USD_TO_VND * OZ_TO_CHI)
        return None
    except Exception as e:
        app.logger.warning('GoldAPI error (date=%s): %s', date_str, e)
        return None


def backfill_world_gold(days=15):
    """Backfill world gold prices for the last `days` days."""
    inserted = 0
    today = datetime.now(VN_TZ).date()
    for i in range(days):
        day = today - timedelta(days=i)
        date_str = day.strftime('%Y%m%d')
        # Midday VN timestamp for that day
        ts = int(datetime(day.year, day.month, day.day, 12, 0, 0, tzinfo=VN_TZ).timestamp())
        # Skip if already stored
        db = sqlite3.connect(DB_PATH)
        c = db.cursor()
        c.execute('SELECT COUNT(1) FROM prices WHERE gold_type=? AND date(timestamp+?,'"'"'unixepoch'"'"')=date(?+?,'"'"'unixepoch'"'"')',
                  ('WORLD', VN_OFFSET_SECS, ts, VN_OFFSET_SECS))
        exists = c.fetchone()[0]
        db.close()
        if exists:
            app.logger.info('World gold %s already in DB', date_str)
            continue
        price_vnd = fetch_world_gold(date_str)
        if price_vnd:
            insert_price(ts, 'WORLD', price_vnd, price_vnd)  # buy=sell=world price
            inserted += 1
            app.logger.info('Backfilled world gold %s: %d VND/chi', date_str, price_vnd)
        else:
            app.logger.warning('No world gold data for %s', date_str)
        time.sleep(0.3)
    return inserted


def backfill_world_intraday_24h() -> int:
    """
    Backfill intraday world gold prices for the last 24 hours using yfinance (GC=F).
    """
    inserted = 0
    try:
        tk = yf.Ticker('GC=F')
        # Fetch last 1 day, 1 hour intervals to get a good intraday curve
        df = tk.history(period='1d', interval='1h')
        if df.empty:
            app.logger.warning('yfinance returned no data for GC=F')
            return 0
        
        db = sqlite3.connect(DB_PATH)
        for ts_idx, row in df.iterrows():
            # Convert pandas timestamp to UTC, then get unix timestamp
            ts_unix = int(ts_idx.timestamp())
            price_usd = row['Close']
            price_vnd = round(float(price_usd) * USD_TO_VND * OZ_TO_CHI)
            
            c = db.cursor()
            c.execute('SELECT COUNT(1) FROM prices WHERE timestamp = ? AND gold_type = ?',
                      (ts_unix, 'WORLD'))
            exists = c.fetchone()[0]
            if not exists:
                c.execute('INSERT INTO prices(timestamp, gold_type, buy, sell) VALUES(?, ?, ?, ?)',
                          (ts_unix, 'WORLD', price_vnd, price_vnd))
                if c.rowcount > 0:
                    inserted += 1
        db.commit()
        db.close()
        app.logger.info('Backfilled %d intraday world gold ticks (24h)', inserted)
    except Exception as e:
        app.logger.error('Failed to backfill world gold intraday: %s', e)
    return inserted


# -------------------- Web endpoints --------------------
@app.route('/api/prices')
def api_prices():
    mode = request.args.get('mode', '7d')
    gold_type = request.args.get('type', 'SJC').upper()
    if gold_type not in SUPPORTED_TYPES_QUERY:
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


@app.route('/api/oil-prices')
def api_oil_prices():
    """Return oil prices from DB. mode=today|7d|30d."""
    mode = request.args.get('mode', '7d')
    if mode == 'today':
        days = 1
    elif mode == '30d':
        days = 30
    else:
        days = 7

    db = get_db_conn()
    c = db.cursor()
    c.execute(
        'SELECT date, name, price, change FROM oil_prices ORDER BY date DESC LIMIT ?',
        (days * 20,)  # upper bound: 20 products × days
    )
    rows = c.fetchall()
    db.close()

    # Group by date, sorted ASC
    from collections import defaultdict
    by_date = defaultdict(list)
    for r in rows:
        by_date[r['date']].append({
            'name': r['name'],
            'price': r['price'],
            'change': r['change']
        })

    # Latest day's prices for the table
    sorted_dates = sorted(by_date.keys())
    latest_date = sorted_dates[-1] if sorted_dates else None
    latest_items = by_date[latest_date] if latest_date else []

    # Time-series: average price per day (or per product for charting)
    # Return full by_date dict so frontend can choose
    history = [
        {'date': d, 'items': by_date[d]}
        for d in sorted_dates
    ]

    return jsonify({
        'status': 'ok',
        'latest_date': latest_date,
        'latest': latest_items,
        'history': history
    })


@app.route('/api/fetch-oil-history', methods=['POST'])
def api_fetch_oil_history():
    try:
        days = int(request.args.get('days', 15))
    except Exception:
        days = 15
    inserted = backfill_oil_prices(days)
    return jsonify({'status': 'ok', 'inserted': inserted})


@app.route('/api/fetch-world-gold', methods=['POST'])
def api_fetch_world_gold():
    try:
        days = int(request.args.get('days', 15))
    except Exception:
        days = 15
    inserted = backfill_world_gold(days)
    return jsonify({'status': 'ok', 'inserted': inserted})


@app.route('/api/fetch-world-24h', methods=['POST'])
def api_fetch_world_24h():
    inserted = backfill_world_intraday_24h()
    return jsonify({'status': 'ok', 'inserted': inserted})


@app.route('/')
def index():
    return render_template('index.html')


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Gold & Oil price service")
    parser.add_argument('--port', type=int, default=int(os.getenv('PORT', 3000)),
                        help='Port to run the Flask app (default from PORT env or 3000)')
    parser.add_argument('--backfill-15d', action='store_true',
                        help='Backfill the last 15 days of historical data from Mi Hong API')
    parser.add_argument('--backfill-24h', action='store_true',
                        help='Backfill only today intraday ticks (via ?last=24h)')
    parser.add_argument('--backfill-oil', action='store_true',
                        help='Backfill the last 15 days of oil price data from PVOil')
    parser.add_argument('--backfill-world', action='store_true',
                        help='Backfill the last 15 days of world gold prices from GoldAPI')
    parser.add_argument('--backfill-world-24h', action='store_true',
                        help='Backfill the intraday world gold prices for the last 24h using yfinance')
    parser.add_argument('--fetch', action='store_true',
                        help='Fetch current prices once immediately on startup')
    args = parser.parse_args()

    # Initialize DB
    init_db()

    # Backfill history on startup if explicitly requested
    if args.backfill_15d:
        app.logger.info("Backfilling last 15 days from Mi Hồng history API...")
        inserted = backfill_from_history(15)
        app.logger.info("15-day backfill complete: %d records inserted", inserted)
    elif args.backfill_24h:
        app.logger.info("Backfilling only today's intraday data...")
        inserted = backfill_intraday_today()
        app.logger.info("Intraday backfill complete: %d records inserted", inserted)

    if args.backfill_oil:
        app.logger.info("Backfilling last 15 days of oil prices from PVOil...")
        inserted = backfill_oil_prices(15)
        app.logger.info("Oil backfill complete: %d records inserted", inserted)

    if args.backfill_world:
        app.logger.info("Backfilling last 15 days of world gold from GoldAPI...")
        inserted = backfill_world_gold(15)
        app.logger.info("World gold backfill complete: %d records inserted", inserted)

    if args.backfill_world_24h:
        app.logger.info("Backfilling intraday world gold (24h) from yfinance...")
        inserted = backfill_world_intraday_24h()
        app.logger.info("World gold intraday complete: %d records inserted", inserted)

    if args.fetch:
        app.logger.info("Running initial fetch for current prices...")
        job_fetch_and_store()

    # Run Flask app
    app.run(host='0.0.0.0', port=args.port, debug=False)
