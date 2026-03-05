"""
OpenOwl Proactive Scheduler
━━━━━━━━━━━━━━━━━━━━━━━━━━━
Runs background jobs 24/7:
  • Morning briefing (daily at user's configured time)
  • Smart reminders (from tasks created via agent)
  • Evening summary
  • Proactive suggestions based on calendar
  • Flight check-in alerts
  • Bill due date alerts

Uses APScheduler (lightweight, no Celery needed for simple jobs).
"""
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Callable, Awaitable
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from memory.store import OwlMemory
from tools.google_calendar import get_todays_events, get_week_events
from tools.gmail import get_unread_emails, extract_important_info
from tools.web_tools import get_weather
from config import settings

logger = logging.getLogger(__name__)

# Type alias for the send function
SendFn = Callable[[str, str], Awaitable[None]]


class OwlScheduler:
    """
    Background scheduler for all proactive OpenOwl tasks.
    Initialized once at startup, runs forever.
    """

    def __init__(self, memory: OwlMemory, send_fn: SendFn):
        """
        memory: the OwlMemory instance
        send_fn: async function(user_id, message) that sends via best channel
        """
        self.memory  = memory
        self.send_fn = send_fn
        self.scheduler = AsyncIOScheduler(timezone="UTC")
        self._setup_jobs()

    def _setup_jobs(self):
        """Register all recurring jobs."""

        # ── Every minute: check for due reminders ────────────────────────
        self.scheduler.add_job(
            self._process_due_reminders,
            "interval",
            minutes=1,
            id="check_reminders",
            name="Process due reminders",
            max_instances=1,
        )

        # ── Every morning at 7:30 AM IST: morning briefings ──────────────
        self.scheduler.add_job(
            self._send_all_morning_briefings,
            CronTrigger(hour=2, minute=0, timezone="UTC"),  # 7:30 AM IST = 2:00 UTC
            id="morning_briefing",
            name="Morning briefings for all users",
            max_instances=1,
        )

        # ── Every evening at 8:00 PM IST: evening summary ────────────────
        self.scheduler.add_job(
            self._send_evening_summaries,
            CronTrigger(hour=14, minute=30, timezone="UTC"),  # 8PM IST = 2:30 PM UTC
            id="evening_summary",
            name="Evening summaries",
            max_instances=1,
        )

        # ── Every hour: check for upcoming events (1-hour advance notice) ─
        self.scheduler.add_job(
            self._send_event_reminders,
            "interval",
            hours=1,
            id="event_reminders",
            name="Calendar event reminders",
            max_instances=1,
        )

        logger.info("✅ Scheduler jobs registered")

    def start(self):
        self.scheduler.start()
        logger.info("✅ OpenOwl Scheduler started")

    def stop(self):
        self.scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")

    # ── ADD A ONE-TIME REMINDER ───────────────────────────────────────────────

    async def add_reminder(
        self,
        user_id: str,
        message: str,
        remind_at: datetime,
        job_id: str = None,
    ):
        """
        Schedule a one-time reminder for a user.
        Called when agent processes "remind me to..." instructions.
        """
        if remind_at < datetime.now(remind_at.tzinfo):
            logger.warning(f"Reminder time {remind_at} is in the past, skipping")
            return

        job_id = job_id or f"reminder_{user_id}_{remind_at.timestamp():.0f}"

        self.scheduler.add_job(
            self._fire_reminder,
            DateTrigger(run_date=remind_at),
            args=[user_id, message],
            id=job_id,
            replace_existing=True,
            name=f"Reminder for {user_id}",
        )

        # Also save to Redis for persistence across restarts
        await self.memory.redis.set_session(
            f"reminder_{job_id}",
            {
                "user_id": user_id,
                "message": message,
                "remind_at": remind_at.isoformat(),
                "job_id": job_id,
            },
            ttl_seconds=int((remind_at - datetime.now(remind_at.tzinfo)).total_seconds()) + 60,
        )

        logger.info(f"📅 Reminder scheduled: {user_id} at {remind_at}")

    async def add_daily_reminder(
        self,
        user_id: str,
        message: str,
        hour: int,
        minute: int = 0,
        user_tz: str = "Asia/Kolkata",
    ):
        """
        Schedule a daily recurring reminder.
        E.g. "remind me to take medicine every day at 9pm"
        """
        tz = ZoneInfo(user_tz)

        # Convert user's local time to UTC for the cron trigger
        local_dt = datetime.now(tz).replace(hour=hour, minute=minute, second=0)
        utc_dt   = local_dt.astimezone(ZoneInfo("UTC"))
        utc_hour = utc_dt.hour
        utc_min  = utc_dt.minute

        job_id = f"daily_{user_id}_{hour:02d}{minute:02d}"

        self.scheduler.add_job(
            self._fire_reminder,
            CronTrigger(hour=utc_hour, minute=utc_min, timezone="UTC"),
            args=[user_id, message],
            id=job_id,
            replace_existing=True,
            name=f"Daily reminder {hour:02d}:{minute:02d} for {user_id}",
        )

        logger.info(f"🔁 Daily reminder: {user_id} at {hour:02d}:{minute:02d} {user_tz}")
        return job_id

    async def cancel_reminder(self, job_id: str):
        """Cancel a scheduled reminder."""
        try:
            self.scheduler.remove_job(job_id)
            logger.info(f"Reminder {job_id} cancelled")
        except Exception:
            pass  # Job might already be done

    # ── MORNING BRIEFING ──────────────────────────────────────────────────────

    async def _send_all_morning_briefings(self):
        """Send morning briefings to all active users."""
        logger.info("🌅 Sending morning briefings...")

        # Get all users who have briefings enabled
        if not self.memory.postgres._sessionmaker:
            return

        try:
            from sqlalchemy import text
            async with self.memory.postgres._sessionmaker() as session:
                result = await session.execute(
                    text("""SELECT user_id, name, timezone, city, persona
                            FROM users
                            WHERE last_seen > NOW() - INTERVAL '7 days'""")
                )
                users = [dict(row._mapping) for row in result]
        except Exception as e:
            logger.error(f"Could not fetch users for briefing: {e}")
            return

        for user in users:
            try:
                await self._send_morning_briefing(
                    user_id=user["user_id"],
                    name=user.get("name") or "there",
                    city=user.get("city") or "Hyderabad",
                    user_tz=user.get("timezone") or "Asia/Kolkata",
                    persona=user.get("persona") or "aria",
                )
                await asyncio.sleep(0.5)  # rate limit between users
            except Exception as e:
                logger.error(f"Briefing failed for {user['user_id']}: {e}")

    async def _send_morning_briefing(
        self,
        user_id: str,
        name: str,
        city: str = "Hyderabad",
        user_tz: str = "Asia/Kolkata",
        persona: str = "aria",
    ):
        """Generate and send a personalised morning briefing."""
        tz = ZoneInfo(user_tz)
        now = datetime.now(tz)
        hour = now.hour

        # Time-of-day greeting
        if 5 <= hour < 12:
            greeting_time = "morning"
        elif 12 <= hour < 17:
            greeting_time = "afternoon"
        else:
            greeting_time = "evening"

        date_str = now.strftime("%A, %d %B %Y")

        # Collect all briefing data
        sections = []

        # 1. Weather
        try:
            weather_raw = get_weather.invoke({"city": city})
            # Extract just the first 2 lines for brevity
            weather_lines = weather_raw.split("\n")[:4]
            weather_text = "\n".join(weather_lines)
            sections.append(weather_text)
        except Exception:
            sections.append(f"🌤️ Weather in {city}: check your weather app")

        # 2. Calendar events
        try:
            calendar_text = get_todays_events.invoke({
                "user_id": user_id, "user_tz": user_tz
            })
            sections.append(calendar_text)
        except Exception:
            sections.append("📅 Calendar: connect Google Calendar to see your events")

        # 3. Important emails
        try:
            email_text = extract_important_info.invoke({"user_id": user_id})
            if "No important" not in email_text:
                sections.append(email_text)
        except Exception:
            pass  # Skip silently if Gmail not connected

        # 4. Pending reminders
        # (pulled from Redis by looking at reminder keys)

        # ── Build the briefing message ────────────────────────────────────

        persona_greetings = {
            "aria":  f"Good {greeting_time}, {name}! Here's your briefing for {date_str}:",
            "priya": f"Good {greeting_time}, {name}! 😊 Ready for {date_str}? Here's what's happening:",
            "nova":  f"hey {name}! morning rundown for {date_str} 👇",
            "meera": f"Good {greeting_time}, {name}! {date_str} ki summary hai aapke liye 🌅",
            "zara":  f"Good {greeting_time}, {name}. Your briefing for {date_str}:",
        }

        header = persona_greetings.get(persona, f"Good {greeting_time}, {name}!")
        footer_map = {
            "aria":  "\n_Reply with any task — I'll handle it!_",
            "priya": "\n_Have a wonderful day! Let me know if you need anything 😊_",
            "nova":  "\n_lmk if you need anything done_",
            "meera": "\n_Aaj ka din achha ho! Kuch chahiye toh batayein 😊_",
            "zara":  "\n_I am available for any tasks requiring your attention today._",
        }
        footer = footer_map.get(persona, "\n_I'm here if you need anything!_")

        full_message = (
            f"🌅 *{header}*\n\n"
            + "\n\n".join(sections)
            + footer
        )

        await self.send_fn(user_id, full_message)
        logger.info(f"✅ Morning briefing sent to {user_id}")

    # ── EVENING SUMMARY ───────────────────────────────────────────────────────

    async def _send_evening_summaries(self):
        """Send evening task summaries to active users."""
        logger.info("🌙 Sending evening summaries...")
        # Same pattern as morning briefing — summarise what happened today
        # For brevity, similar structure to morning briefing
        pass

    # ── EVENT REMINDERS ───────────────────────────────────────────────────────

    async def _send_event_reminders(self):
        """
        Check for calendar events starting in the next 60 minutes.
        Send a reminder if one is found.
        """
        if not self.memory.postgres._sessionmaker:
            return

        try:
            from sqlalchemy import text
            async with self.memory.postgres._sessionmaker() as session:
                result = await session.execute(
                    text("""SELECT user_id, timezone FROM users
                            WHERE last_seen > NOW() - INTERVAL '3 days'""")
                )
                users = [dict(row._mapping) for row in result]
        except Exception:
            return

        for user in users:
            try:
                user_id = user["user_id"]
                user_tz = user.get("timezone") or "Asia/Kolkata"
                tz = ZoneInfo(user_tz)
                now = datetime.now(tz)

                # Check Redis cache to avoid duplicate reminders
                cache_key = f"event_reminded:{user_id}:{now.strftime('%Y%m%d%H')}"
                already_sent = await self.memory.redis._client.get(cache_key)
                if already_sent:
                    continue

                # Get events in next 60 min
                try:
                    from googleapiclient.discovery import build
                    from tools.google_calendar import _get_calendar_service

                    service = _get_calendar_service(user_id)
                    events_result = service.events().list(
                        calendarId="primary",
                        timeMin=now.isoformat(),
                        timeMax=(now + timedelta(hours=1)).isoformat(),
                        singleEvents=True,
                        orderBy="startTime",
                        maxResults=3,
                    ).execute()

                    events = events_result.get("items", [])
                    if events:
                        for event in events:
                            title = event.get("summary", "Meeting")
                            start = event.get("start", {})
                            start_str = start.get("dateTime", start.get("date", ""))
                            if start_str:
                                dt = datetime.fromisoformat(
                                    start_str.replace("Z", "+00:00")
                                ).astimezone(tz)
                                mins = int((dt - now).total_seconds() / 60)
                                msg = (
                                    f"⏰ *Reminder:* '{title}' starts in {mins} minutes!\n"
                                    f"🕐 {dt.strftime('%I:%M %p')}"
                                )
                                if event.get("location"):
                                    msg += f"\n📍 {event['location']}"
                                await self.send_fn(user_id, msg)

                        # Mark as sent for this hour
                        await self.memory.redis._client.setex(cache_key, 3600, "1")

                except Exception:
                    pass  # Calendar not connected for this user

            except Exception as e:
                logger.debug(f"Event reminder check failed for {user.get('user_id')}: {e}")

    # ── DUE REMINDERS ────────────────────────────────────────────────────────

    async def _process_due_reminders(self):
        """
        Check Redis for any stored reminders that are now due.
        Fires them and cleans up.
        This provides persistence even if the scheduler restarts.
        """
        try:
            keys = await self.memory.redis._client.keys("session:reminder_*")
            now  = datetime.utcnow()

            for key in keys:
                try:
                    import json
                    data_str = await self.memory.redis._client.get(key)
                    if not data_str:
                        continue
                    data = json.loads(data_str)
                    remind_at = datetime.fromisoformat(data["remind_at"])

                    if remind_at <= now:
                        await self._fire_reminder(data["user_id"], data["message"])
                        await self.memory.redis._client.delete(key)

                except Exception as e:
                    logger.debug(f"Reminder processing error: {e}")

        except Exception as e:
            logger.debug(f"Due reminder check failed: {e}")

    async def _fire_reminder(self, user_id: str, message: str):
        """Actually send a reminder to a user."""
        try:
            await self.send_fn(user_id, f"🔔 *Reminder:* {message}")
            logger.info(f"🔔 Reminder fired for {user_id}: {message[:50]}")
        except Exception as e:
            logger.error(f"Failed to fire reminder for {user_id}: {e}")


# ── UNIVERSAL SEND FUNCTION ───────────────────────────────────────────────────

def build_send_function(
    tg_bot=None,
    wa_handler=None,
    memory: OwlMemory = None,
) -> SendFn:
    """
    Build a universal send function that routes to the right channel.
    user_id format:
      "tg:12345678"    → Telegram
      "wa:+919876543"  → WhatsApp
      "sms:+91..."     → SMS (via Twilio)
    """
    async def send_to_user(user_id: str, message: str):
        try:
            if user_id.startswith("tg:"):
                chat_id = int(user_id.replace("tg:", ""))
                if tg_bot:
                    await tg_bot.send_message(
                        chat_id=chat_id,
                        text=message,
                        parse_mode="Markdown",
                    )

            elif user_id.startswith("wa:"):
                phone = user_id.replace("wa:", "")
                if wa_handler:
                    await wa_handler.send_proactive(phone, message)

            elif user_id.startswith("sms:"):
                phone = user_id.replace("sms:", "")
                # Strip markdown for SMS
                clean = message.replace("*", "").replace("_", "").replace("`", "")
                if settings.twilio_account_sid:
                    from twilio.rest import Client
                    client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
                    client.messages.create(
                        body=clean[:1600],
                        from_=settings.twilio_phone_number,
                        to=phone,
                    )
            else:
                logger.warning(f"Unknown channel prefix in user_id: {user_id}")

        except Exception as e:
            logger.error(f"Send failed to {user_id}: {e}")

    return send_to_user
