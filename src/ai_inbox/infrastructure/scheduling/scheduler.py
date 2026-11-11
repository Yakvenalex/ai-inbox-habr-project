from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger


def make_scheduler(settings, digests, media) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=settings.app_timezone)

    async def daily():
        await digests(scheduled=True)

    if settings.digest_enabled:
        scheduler.add_job(
            daily,
            CronTrigger(
                hour=settings.digest_hour,
                minute=settings.digest_minute,
                timezone=settings.app_timezone,
            ),
            id="daily-digest",
            coalesce=True,
            max_instances=1,
            misfire_grace_time=settings.digest_misfire_grace_seconds,
        )
    scheduler.add_job(
        media.cleanup,
        "interval",
        hours=6,
        id="cleanup-audio",
        coalesce=True,
        max_instances=1,
        misfire_grace_time=3600,
    )
    return scheduler
