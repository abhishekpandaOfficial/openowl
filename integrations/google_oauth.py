"""
OpenOwl Google OAuth Integration
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Handles OAuth2 flow for:
  - Google Calendar
  - Gmail

Users click a link → authorize in browser → tokens saved → Owl can access their apps.
Tokens auto-refresh, so users only need to do this once.
"""
import logging
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse

from memory.store import OwlMemory
from config import settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["OAuth"])

# Combined scopes for both Calendar and Gmail
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
]

CLIENT_CONFIG = {
    "web": {
        "client_id":     settings.google_client_id,
        "client_secret": settings.google_client_secret,
        "auth_uri":      "https://accounts.google.com/o/oauth2/auth",
        "token_uri":     "https://oauth2.googleapis.com/token",
        "redirect_uris": [settings.google_redirect_uri],
    }
}


@router.get("/google/connect/{user_id}", response_class=HTMLResponse)
async def google_connect_page(user_id: str, request: Request):
    """
    Serve the Google connection page.
    Users get this link from the bot when they ask to connect Gmail/Calendar.
    """
    memory: OwlMemory = request.app.state.memory

    # Check if already connected
    user_ctx = await memory.postgres.get_or_create_user(user_id, "web")
    connected_apps = user_ctx.get("connected_apps", [])
    google_connected = "google" in connected_apps

    return HTMLResponse(f"""
<!DOCTYPE html>
<html>
<head>
  <title>Connect Google — OpenOwl</title>
  <meta charset="UTF-8">
  <style>
    body {{ background:#04060f; color:#d4deff; font-family:monospace;
           display:flex; align-items:center; justify-content:center;
           min-height:100vh; margin:0; }}
    .card {{ background:#080d1c; border:1px solid #1a2540; border-radius:16px;
             padding:40px; max-width:400px; text-align:center; }}
    h1 {{ color:#4d9fff; margin-bottom:8px; font-size:24px; }}
    .owl {{ font-size:48px; margin-bottom:16px; }}
    p {{ color:#6b7db3; margin-bottom:24px; line-height:1.6; font-size:13px; }}
    .btn {{ display:inline-block; background:#4d9fff; color:#fff;
            border-radius:8px; padding:14px 28px; text-decoration:none;
            font-weight:bold; font-size:14px; transition:all 0.2s; }}
    .btn:hover {{ background:#6eb3ff; }}
    .connected {{ color:#00e5a0; font-size:13px; margin-top:16px; }}
    .perms {{ text-align:left; margin:16px 0; font-size:12px; color:#4a5880; }}
    .perms li {{ margin:4px 0; }}
  </style>
</head>
<body>
  <div class="card">
    <div class="owl">🦉</div>
    <h1>Connect Google</h1>
    <p>Allow OpenOwl to access your Google account to manage your calendar and email.</p>

    <div class="perms">OpenOwl will be able to:
      <ul>
        <li>✅ Read your calendar events</li>
        <li>✅ Create and update calendar events</li>
        <li>✅ Read your emails (inbox only)</li>
        <li>✅ Send emails (only with your approval)</li>
      </ul>
      <b>OpenOwl will NEVER:</b>
      <ul>
        <li>❌ Delete emails without asking</li>
        <li>❌ Access other Google apps</li>
        <li>❌ Share your data</li>
      </ul>
    </div>

    {'<div class="connected">✅ Google already connected!</div>' if google_connected else
     f'<a class="btn" href="/auth/google/authorize?user_id={user_id}">Connect Google Account</a>'}
  </div>
</body>
</html>
""")


@router.get("/google/authorize")
async def google_authorize(user_id: str):
    """Redirect user to Google's OAuth consent screen."""
    if not settings.google_client_id:
        raise HTTPException(
            status_code=503,
            detail="Google OAuth not configured. Set GOOGLE_CLIENT_ID in .env"
        )

    try:
        from google_auth_oauthlib.flow import Flow

        flow = Flow.from_client_config(CLIENT_CONFIG, scopes=GOOGLE_SCOPES)
        flow.redirect_uri = settings.google_redirect_uri

        auth_url, _ = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            state=user_id,
            prompt="consent",
        )
        return RedirectResponse(url=auth_url)

    except ImportError:
        raise HTTPException(
            status_code=503,
            detail="Run: pip install google-auth-oauthlib google-api-python-client"
        )


@router.get("/google/callback", response_class=HTMLResponse)
async def google_callback(
    request: Request,
    code: str = None,
    state: str = None,  # = user_id
    error: str = None,
):
    """
    Handle Google OAuth callback.
    Exchange code for tokens, save them, notify user via their channel.
    """
    if error:
        return HTMLResponse(_result_page(
            False, "Authorization was cancelled or failed.",
            detail=error
        ))

    if not code or not state:
        return HTMLResponse(_result_page(False, "Missing authorization code."))

    user_id = state
    memory: OwlMemory = request.app.state.memory

    try:
        from google_auth_oauthlib.flow import Flow
        import os

        flow = Flow.from_client_config(CLIENT_CONFIG, scopes=GOOGLE_SCOPES)
        flow.redirect_uri = settings.google_redirect_uri
        flow.fetch_token(code=code)
        creds = flow.credentials

        # Save token for Calendar
        cal_path = f"data/google_calendar_token_{user_id}.json"
        os.makedirs("data", exist_ok=True)
        with open(cal_path, "w") as f:
            f.write(creds.to_json())

        # Save token for Gmail (same token covers both)
        gmail_path = f"data/google_gmail_token_{user_id}.json"
        with open(gmail_path, "w") as f:
            f.write(creds.to_json())

        # Update user's connected apps in DB
        user = await memory.postgres.get_or_create_user(user_id, "web")
        connected = user.get("connected_apps", [])
        if "google" not in connected:
            connected.append("google")
        if "gmail" not in connected:
            connected.append("gmail")
        if "calendar" not in connected:
            connected.append("calendar")

        from sqlalchemy import text
        import json
        async with memory.postgres._sessionmaker() as session:
            await session.execute(
                text("UPDATE users SET connected_apps = :apps WHERE user_id = :uid"),
                {"apps": json.dumps(connected), "uid": user_id}
            )
            await session.commit()

        # Notify user on their channel
        success_msg = (
            "✅ *Google account connected!*\n\n"
            "I can now:\n"
            "📅 Read and manage your Google Calendar\n"
            "📧 Read your Gmail and draft emails (with your approval)\n\n"
            "Try: _\"What meetings do I have today?\"_ or _\"Check my unread emails\"_"
        )

        # Try to send via Telegram first, then WhatsApp
        if request.app.state.telegram_app:
            try:
                tg_id = user_id.replace("tg:", "")
                if tg_id.isdigit():
                    await request.app.state.telegram_app.bot.send_message(
                        chat_id=int(tg_id),
                        text=success_msg,
                        parse_mode="Markdown",
                    )
            except Exception as e:
                logger.warning(f"Could not notify user {user_id}: {e}")

        logger.info(f"✅ Google connected for user {user_id}")
        return HTMLResponse(_result_page(True, "Google account connected successfully!"))

    except Exception as e:
        logger.error(f"Google OAuth callback failed: {e}", exc_info=True)
        return HTMLResponse(_result_page(False, "Connection failed.", detail=str(e)))


def _result_page(success: bool, message: str, detail: str = "") -> str:
    """Generate the result HTML page shown after OAuth."""
    emoji = "✅" if success else "❌"
    color = "#00e5a0" if success else "#ff5f7e"
    return f"""
<!DOCTYPE html>
<html>
<head><title>{'Success' if success else 'Error'} — OpenOwl</title><meta charset="UTF-8">
<style>
  body {{ background:#04060f; color:#d4deff; font-family:monospace;
         display:flex; align-items:center; justify-content:center; min-height:100vh; }}
  .card {{ background:#080d1c; border:1px solid #1a2540; border-radius:16px;
           padding:40px; max-width:380px; text-align:center; }}
  h1 {{ color:{color}; }}
  p {{ color:#6b7db3; font-size:13px; }}
  .close {{ color:#4a5880; margin-top:20px; font-size:12px; }}
</style></head>
<body><div class="card">
  <div style="font-size:48px">{emoji}</div>
  <h1>{'Success!' if success else 'Oops!'}</h1>
  <p>{message}</p>
  {f'<p style="color:#ff5f7e;font-size:11px">{detail}</p>' if detail else ''}
  <p class="close">You can close this window and return to OpenOwl.</p>
</div></body></html>
"""
