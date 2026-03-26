from app import job_fetch_and_store, init_db
from apscheduler.schedulers.blocking import BlockingScheduler

if __name__ == "__main__":
    init_db()
    print(">>> Starting scheduler (every 30 minutes)")
    scheduler = BlockingScheduler()
    scheduler.add_job(job_fetch_and_store, 'cron', minute='0,30', timezone='Asia/Ho_Chi_Minh')
    scheduler.start()
