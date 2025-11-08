from app import job_fetch_and_store, init_db, fetch_price_for_date, insert_price
from apscheduler.schedulers.blocking import BlockingScheduler

if __name__ == "__main__":
    print(">>> Starting background scheduler (every 30 mins)")
    init_db()
    scheduler = BlockingScheduler()
    scheduler.add_job(job_fetch_and_store, 'interval', hours=0.5)
    scheduler.start()
