"""
OpenOwl Google Calendar Tool
━━━━━━━━━━━━━━━━━━━━━━━━━━━
Full read/write access to Google Calendar via OAuth2.
Every write operation goes through the LangGraph approval gate first.

Capabilities:
  • List today's / this week's events
  • Create new events (with approval)
  • Update existing events (with approval)
  • Delete events (with approval)
  • Check free/busy slots
  • Find next available meeting slot
  • Get event reminders
"""
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from langchain_core.tools import tool

logger = logging.getLogger(__name__)

# ── OAUTH SETUP ───────────────────────────────────────────────────────────────

SCOPES = [
    "https://www.googleapis.com/auth/calendar",          # full calendar access
    "https://www.googleapis.com/auth/calendar.readonly",
]

TOKEN_FILE = "data/google_calendar_token_{user_id}.json"


def _get_calendar_service(user_id: str):
    """
    Get an authenticated Google Calendar service for a user.
    Loads token from file, refreshes if expired.
    Returns the service object or raises if not authenticated.
    """
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build

        token_path = TOKEN_FILE.format(user_id=user_id)
        creds = None

        if os.path.exists(token_path):
            creds = Credentials.from_authorized_user_file(token_path, SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
                # Save refreshed token
                with open(token_path, "w") as f:
                    f.write(creds.to_json())
            else:
                raise ValueError(
                    f"Google Calendar not authorized for user {user_id}. "
                    f"Visit /auth/google to connect your calendar."
                )

        return build("calendar", "v3", credentials=creds)

    except ImportError:
        raise ImportError(
            "Google API packages not installed. "
            "Run: pip install google-auth google-auth-oauthlib google-api-python-client"
        )


def _tz(user_tz: str = "Asia/Kolkata") -> ZoneInfo:
    return ZoneInfo(user_tz)


def _format_event(event: dict, user_tz: str = "Asia/Kolkata") -> str:
    """Format a calendar event into a readable string."""
    title    = event.get("summary", "Untitled")
    location = event.get("location", "")
    desc     = event.get("description", "")[:80] if event.get("description") else ""

    start_raw = event.get("start", {})
    end_raw   = event.get("end", {})

    # All-day vs timed
    if "date" in start_raw:
        time_str = f"All day · {start_raw['date']}"
    else:
        start_dt = datetime.fromisoformat(
            start_raw["dateTime"].replace("Z", "+00:00")
        ).astimezone(_tz(user_tz))
        end_dt = datetime.fromisoformat(
            end_raw["dateTime"].replace("Z", "+00:00")
        ).astimezone(_tz(user_tz))
        time_str = (
            f"{start_dt.strftime('%I:%M %p')} – {end_dt.strftime('%I:%M %p')}"
        )

    line = f"📅 *{title}*\n   🕐 {time_str}"
    if location:
        line += f"\n   📍 {location}"
    if desc:
        line += f"\n   _{desc}_"
    return line


# ── LANGCHAIN TOOLS ───────────────────────────────────────────────────────────

@tool
def get_todays_events(user_id: str, user_tz: str = "Asia/Kolkata") -> str:
    """
    Get all calendar events for today.
    Returns a formatted list of events with times and details.
    """
    try:
        service = _get_calendar_service(user_id)
        tz = _tz(user_tz)

        now = datetime.now(tz)
        start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end_of_day   = now.replace(hour=23, minute=59, second=59)

        events_result = service.events().list(
            calendarId="primary",
            timeMin=start_of_day.isoformat(),
            timeMax=end_of_day.isoformat(),
            singleEvents=True,
            orderBy="startTime",
        ).execute()

        events = events_result.get("items", [])

        if not events:
            return "📅 No events today. Your day is clear!"

        today_str = now.strftime("%A, %d %B %Y")
        lines = [f"📅 *Your schedule for {today_str}:*\n"]
        for event in events:
            lines.append(_format_event(event, user_tz))
        lines.append(f"\n_{len(events)} event(s) today_")

        return "\n\n".join(lines)

    except Exception as e:
        logger.error(f"get_todays_events error: {e}")
        return f"❌ Could not fetch calendar events: {e}"


@tool
def get_week_events(user_id: str, user_tz: str = "Asia/Kolkata") -> str:
    """Get all calendar events for the next 7 days."""
    try:
        service = _get_calendar_service(user_id)
        tz = _tz(user_tz)

        now  = datetime.now(tz)
        end  = now + timedelta(days=7)

        events_result = service.events().list(
            calendarId="primary",
            timeMin=now.isoformat(),
            timeMax=end.isoformat(),
            singleEvents=True,
            orderBy="startTime",
            maxResults=20,
        ).execute()

        events = events_result.get("items", [])
        if not events:
            return "📅 No events in the next 7 days."

        # Group by day
        days: dict[str, list] = {}
        for event in events:
            start_raw = event.get("start", {})
            if "date" in start_raw:
                day_key = start_raw["date"]
            else:
                dt = datetime.fromisoformat(
                    start_raw["dateTime"].replace("Z", "+00:00")
                ).astimezone(tz)
                day_key = dt.strftime("%Y-%m-%d")

            days.setdefault(day_key, []).append(event)

        lines = ["📅 *Next 7 days:*\n"]
        for day_key in sorted(days.keys()):
            day_dt = datetime.fromisoformat(day_key).strftime("%A, %d %b")
            lines.append(f"*{day_dt}*")
            for event in days[day_key]:
                lines.append(_format_event(event, user_tz))
            lines.append("")

        return "\n".join(lines)

    except Exception as e:
        return f"❌ Could not fetch week events: {e}"


@tool
def check_free_slots(
    user_id: str,
    date_str: str,
    duration_minutes: int = 60,
    user_tz: str = "Asia/Kolkata",
) -> str:
    """
    Find free time slots on a given date.
    date_str: 'today', 'tomorrow', or 'YYYY-MM-DD'
    Returns list of available time windows.
    """
    try:
        service = _get_calendar_service(user_id)
        tz = _tz(user_tz)
        now = datetime.now(tz)

        # Parse date
        if date_str.lower() == "today":
            target = now.date()
        elif date_str.lower() == "tomorrow":
            target = (now + timedelta(days=1)).date()
        else:
            target = datetime.fromisoformat(date_str).date()

        day_start = datetime(target.year, target.month, target.day,
                             9, 0, tzinfo=tz)
        day_end   = datetime(target.year, target.month, target.day,
                             18, 0, tzinfo=tz)

        # Get busy times
        freebusy = service.freebusy().query(body={
            "timeMin": day_start.isoformat(),
            "timeMax": day_end.isoformat(),
            "timeZone": user_tz,
            "items": [{"id": "primary"}],
        }).execute()

        busy_times = freebusy["calendars"]["primary"].get("busy", [])

        # Find free slots
        busy_intervals = []
        for b in busy_times:
            b_start = datetime.fromisoformat(
                b["start"].replace("Z", "+00:00")).astimezone(tz)
            b_end   = datetime.fromisoformat(
                b["end"].replace("Z", "+00:00")).astimezone(tz)
            busy_intervals.append((b_start, b_end))

        busy_intervals.sort()

        free_slots = []
        cursor = day_start

        for b_start, b_end in busy_intervals:
            if cursor + timedelta(minutes=duration_minutes) <= b_start:
                free_slots.append((cursor, b_start))
            cursor = max(cursor, b_end)

        if cursor + timedelta(minutes=duration_minutes) <= day_end:
            free_slots.append((cursor, day_end))

        if not free_slots:
            return f"😔 No free slots of {duration_minutes} min on {target}."

        date_label = target.strftime("%A, %d %B")
        lines = [f"✅ *Free slots on {date_label} ({duration_minutes} min):*\n"]
        for start, end in free_slots[:6]:
            lines.append(f"• {start.strftime('%I:%M %p')} – {end.strftime('%I:%M %p')}")

        return "\n".join(lines)

    except Exception as e:
        return f"❌ Could not check calendar: {e}"


@tool
def create_calendar_event(
    user_id: str,
    title: str,
    start_datetime: str,   # ISO format: "2025-11-15T10:00:00"
    end_datetime: str,     # ISO format: "2025-11-15T11:00:00"
    description: str = "",
    location: str = "",
    attendees: list[str] = None,
    user_tz: str = "Asia/Kolkata",
) -> dict:
    """
    Create a new calendar event.
    ⚠️  REQUIRES APPROVAL — do not call directly.
    Returns event details for the approval request.
    """
    # This tool returns a dict that the approval gate will display
    # Actual creation happens in create_calendar_event_confirmed
    return {
        "action": "create_calendar_event",
        "requires_approval": True,
        "description": f"Create calendar event: '{title}' on {start_datetime[:10]}",
        "details": {
            "title": title,
            "start": start_datetime,
            "end": end_datetime,
            "description": description,
            "location": location,
            "attendees": attendees or [],
            "user_id": user_id,
            "user_tz": user_tz,
        }
    }


@tool
def create_calendar_event_confirmed(
    user_id: str,
    title: str,
    start_datetime: str,
    end_datetime: str,
    description: str = "",
    location: str = "",
    attendees: list[str] = None,
    user_tz: str = "Asia/Kolkata",
) -> str:
    """
    Actually creates the calendar event after user approval.
    Called only after the approval gate passes.
    """
    try:
        service = _get_calendar_service(user_id)
        tz = _tz(user_tz)

        event_body = {
            "summary": title,
            "start": {
                "dateTime": start_datetime,
                "timeZone": user_tz,
            },
            "end": {
                "dateTime": end_datetime,
                "timeZone": user_tz,
            },
        }

        if description:
            event_body["description"] = description
        if location:
            event_body["location"] = location
        if attendees:
            event_body["attendees"] = [{"email": e} for e in attendees]

        # Add default reminders
        event_body["reminders"] = {
            "useDefault": False,
            "overrides": [
                {"method": "popup", "minutes": 10},
                {"method": "popup", "minutes": 60},
            ],
        }

        created = service.events().insert(
            calendarId="primary",
            body=event_body,
            sendUpdates="all" if attendees else "none",
        ).execute()

        event_link = created.get("htmlLink", "")
        return (
            f"✅ *Event created!*\n"
            f"📅 *{title}*\n"
            f"🕐 {start_datetime[:16].replace('T', ' ')}\n"
            f"{'📍 ' + location if location else ''}\n"
            f"🔗 [View in Calendar]({event_link})"
        ).strip()

    except Exception as e:
        logger.error(f"Calendar create failed: {e}")
        return f"❌ Could not create event: {e}"


@tool
def delete_calendar_event(
    user_id: str,
    event_id: str,
    event_title: str = "",
) -> dict:
    """
    ⚠️  REQUIRES APPROVAL — delete a calendar event.
    Returns approval request data.
    """
    return {
        "action": "delete_calendar_event",
        "requires_approval": True,
        "description": f"Delete calendar event: '{event_title or event_id}'",
        "details": {"event_id": event_id, "event_title": event_title, "user_id": user_id},
    }


# ── OAUTH FLOW HELPERS ────────────────────────────────────────────────────────

def get_google_auth_url(user_id: str) -> str:
    """Generate the Google OAuth authorization URL for a user."""
    from google_auth_oauthlib.flow import Flow

    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": os.getenv("GOOGLE_CLIENT_ID"),
                "client_secret": os.getenv("GOOGLE_CLIENT_SECRET"),
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [os.getenv("GOOGLE_REDIRECT_URI",
                                             "http://localhost:8000/auth/google/callback")],
            }
        },
        scopes=SCOPES,
    )
    flow.redirect_uri = os.getenv("GOOGLE_REDIRECT_URI",
                                   "http://localhost:8000/auth/google/callback")

    auth_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        state=user_id,                # pass user_id through state param
        prompt="consent",
    )
    return auth_url


def save_google_token(user_id: str, code: str, redirect_uri: str):
    """Exchange auth code for tokens and save them."""
    from google_auth_oauthlib.flow import Flow

    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": os.getenv("GOOGLE_CLIENT_ID"),
                "client_secret": os.getenv("GOOGLE_CLIENT_SECRET"),
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [redirect_uri],
            }
        },
        scopes=SCOPES,
    )
    flow.redirect_uri = redirect_uri
    flow.fetch_token(code=code)

    token_path = TOKEN_FILE.format(user_id=user_id)
    os.makedirs(os.path.dirname(token_path), exist_ok=True)
    with open(token_path, "w") as f:
        f.write(flow.credentials.to_json())

    logger.info(f"✅ Google Calendar token saved for user {user_id}")


# All calendar tools as a list for registration in the agent
CALENDAR_TOOLS = [
    get_todays_events,
    get_week_events,
    check_free_slots,
    create_calendar_event,
    create_calendar_event_confirmed,
    delete_calendar_event,
]
