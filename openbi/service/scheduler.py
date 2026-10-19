"""Scheduled dataset refresh (APScheduler cron jobs)."""

from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from .db import Dataset

log = logging.getLogger("openbi.scheduler")


class RefreshScheduler:
    def __init__(self, store, sessions: sessionmaker, start: bool = True):
        self.store = store
        self.scheduler = BackgroundScheduler(timezone="UTC", job_defaults={"coalesce": True, "max_instances": 1})
        if start:
            self.scheduler.start()
            with sessions() as s:
                for d in s.scalars(select(Dataset).where(Dataset.refresh_schedule.is_not(None))):
                    try:
                        self.schedule(d.id, d.refresh_schedule)
                    except ValueError:
                        log.warning("dataset %s has an invalid schedule %r", d.id, d.refresh_schedule)

    @staticmethod
    def trigger(cron: str) -> CronTrigger:
        return CronTrigger.from_crontab(cron, timezone="UTC")

    def schedule(self, dataset_id: int, cron: str) -> None:
        trigger = self.trigger(cron)  # raises ValueError when invalid
        self.scheduler.add_job(self._run, trigger, args=[dataset_id], id=f"refresh-{dataset_id}", replace_existing=True)

    def unschedule(self, dataset_id: int) -> None:
        job = self.scheduler.get_job(f"refresh-{dataset_id}")
        if job:
            job.remove()

    def _run(self, dataset_id: int) -> None:
        try:
            run = self.store.refresh(dataset_id, trigger="scheduled")
            log.info("scheduled refresh of dataset %s: %s", dataset_id, run.status)
        except Exception:  # noqa: BLE001
            log.exception("scheduled refresh of dataset %s failed", dataset_id)

    def shutdown(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
