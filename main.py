"""
OpenOwl FastAPI Server
Entry point for the entire system.
Handles:
  - Telegram webhook endpoint
  - Twilio WhatsApp/SMS webhooks
  - WebSocket real-time dashboard
  - Health check endpoints
  - Approval resume endpoint
"""
import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

import uvicorn
from fastapi import FastAPI, Request, HTTPException, WebSocket, WebSocketDisconnect, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse

from config import settings
from memory.store import OwlMemory
from channels.telegram_handler import create_telegram_app, set_telegram_webhook
from agent.graph import run_agent

# ── LOGGING SETUP ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO if not settings.debug else logging.DEBUG,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── WEBSOCKET CONNECTION MANAGER ──────────────────────────────────────────────

class ConnectionManager:
    """Manages WebSocket connections for the real-time dashboard."""

    def __init__(self):
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)
        logger.info(f"Dashboard connected ({len(self.active)} active)")

    def disconnect(self, ws: WebSocket):
        self.active.remove(ws)

    async def broadcast(self, data: dict):
        """Send an update to all connected dashboard clients."""
        if not self.active:
            return
        message = json.dumps(data)
        dead = []
        for ws in self.active:
            try:
                await ws.send_text(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.active.remove(ws)


ws_manager = ConnectionManager()


# ── LIFESPAN (startup + shutdown) ─────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize all services on startup, clean up on shutdown."""
    logger.info("🦉 OpenOwl starting up...")

    # 1. Connect to databases
    memory = OwlMemory(
        redis_url=settings.redis_url,
        database_url=settings.database_url,
    )
    await memory.connect()
    app.state.memory = memory

    # 2. Set up Telegram bot
    if settings.telegram_bot_token:
        tg_app = create_telegram_app(memory)
        await tg_app.initialize()
        app.state.telegram_app = tg_app

        # Register webhook if URL configured
        if settings.telegram_webhook_url:
            await set_telegram_webhook(tg_app, settings.telegram_webhook_url)
        else:
            logger.warning(
                "TELEGRAM_WEBHOOK_URL not set. "
                "Set it to your ngrok URL to receive Telegram messages.\n"
                "Run: ngrok http 8000  → copy the https URL → set TELEGRAM_WEBHOOK_URL=<url>"
            )
    else:
        logger.warning("TELEGRAM_BOT_TOKEN not set — Telegram disabled")
        app.state.telegram_app = None

    logger.info("✅ OpenOwl is ready!")
    logger.info(f"   Dashboard: http://localhost:8000")
    logger.info(f"   Health:    http://localhost:8000/health")
    logger.info(f"   Docs:      http://localhost:8000/docs")

    yield

    # Shutdown
    logger.info("🦉 OpenOwl shutting down...")
    await memory.redis.close()
    if app.state.telegram_app:
        await app.state.telegram_app.shutdown()


# ── APP INITIALIZATION ────────────────────────────────────────────────────────

app = FastAPI(
    title="OpenOwl",
    description="Personal Autonomous Agent — WhatsApp · Telegram · SMS · Voice",
    version=settings.app_version,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── HEALTH CHECK ──────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    """Quick health check — used by Railway/Docker healthchecks."""
    memory: OwlMemory = app.state.memory
    redis_ok = False
    try:
        await memory.redis._client.ping()
        redis_ok = True
    except:
        pass

    return {
        "status": "ok",
        "version": settings.app_version,
        "timestamp": datetime.utcnow().isoformat(),
        "services": {
            "redis": "ok" if redis_ok else "error",
            "telegram": "ok" if settings.telegram_bot_token else "not_configured",
            "whatsapp": "ok" if settings.twilio_account_sid else "not_configured",
            "groq": "ok" if settings.groq_api_key else "not_configured",
            "ollama": "enabled" if settings.ollama_enabled else "disabled",
        },
    }


# ── TELEGRAM WEBHOOK ──────────────────────────────────────────────────────────

@app.post("/telegram/webhook")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: Optional[str] = Header(None)
):
    """Receive updates from Telegram and pass to the bot application."""

    # Verify webhook secret
    if x_telegram_bot_api_secret_token != settings.webhook_secret:
        raise HTTPException(status_code=403, detail="Invalid webhook secret")

    tg_app = app.state.telegram_app
    if not tg_app:
        raise HTTPException(status_code=503, detail="Telegram not configured")

    data = await request.json()

    # Process update in background (webhook must return 200 fast)
    from telegram import Update
    update = Update.de_json(data, tg_app.bot)
    await tg_app.process_update(update)

    # Broadcast to dashboard
    await ws_manager.broadcast({
        "type": "telegram_update",
        "data": {"update_id": data.get("update_id")},
        "timestamp": datetime.utcnow().isoformat(),
    })

    return {"ok": True}


# ── TWILIO WHATSAPP WEBHOOK ───────────────────────────────────────────────────

@app.post("/twilio/whatsapp")
async def twilio_whatsapp(request: Request):
    """Receive WhatsApp messages from Twilio."""
    from urllib.parse import parse_qs

    body = await request.body()
    params = parse_qs(body.decode())

    def get_param(key):
        vals = params.get(key, [])
        return vals[0] if vals else ""

    from_number = get_param("From").replace("whatsapp:", "")
    message_body = get_param("Body")
    message_sid = get_param("MessageSid")

    if not from_number or not message_body:
        return {"status": "ignored"}

    memory: OwlMemory = app.state.memory

    # Process through agent
    result = await run_agent(
        user_id=from_number,
        message=message_body,
        channel="whatsapp",
        memory=memory,
        user_name=get_param("ProfileName"),
        channel_message_id=message_sid,
    )

    # Send reply via Twilio
    if result.get("response") and settings.twilio_account_sid:
        await send_twilio_whatsapp(from_number, result["response"])

    # Broadcast to dashboard
    await ws_manager.broadcast({
        "type": "whatsapp_message",
        "data": {
            "from": from_number,
            "message": message_body[:100],
            "response_preview": result.get("response", "")[:100],
        },
        "timestamp": datetime.utcnow().isoformat(),
    })

    # Twilio expects TwiML response (can be empty — we send separately)
    return HTMLResponse(
        content='<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
        media_type="application/xml"
    )


async def send_twilio_whatsapp(to_number: str, message: str):
    """Send a WhatsApp message via Twilio."""
    try:
        from twilio.rest import Client
        client = Client(settings.twilio_account_sid, settings.twilio_auth_token)

        # Split long messages
        max_len = 1600
        chunks = [message[i:i+max_len] for i in range(0, len(message), max_len)]

        for chunk in chunks:
            client.messages.create(
                body=chunk,
                from_=settings.twilio_whatsapp_number,
                to=f"whatsapp:{to_number}",
            )
    except Exception as e:
        logger.error(f"Twilio send failed: {e}")


# ── TWILIO SMS WEBHOOK ────────────────────────────────────────────────────────

@app.post("/twilio/sms")
async def twilio_sms(request: Request):
    """Receive SMS messages from Twilio."""
    from urllib.parse import parse_qs

    body = await request.body()
    params = parse_qs(body.decode())

    def get_param(key):
        vals = params.get(key, [])
        return vals[0] if vals else ""

    from_number = get_param("From")
    message_body = get_param("Body")

    if not from_number or not message_body:
        return {"status": "ignored"}

    memory: OwlMemory = app.state.memory

    result = await run_agent(
        user_id=from_number,
        message=message_body,
        channel="sms",
        memory=memory,
    )

    # Send SMS reply via Twilio (SMS max 160 chars per segment)
    if result.get("response") and settings.twilio_account_sid:
        response_text = result["response"]
        # Strip markdown for SMS
        response_text = response_text.replace("*", "").replace("_", "").replace("`", "")
        try:
            from twilio.rest import Client
            client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
            client.messages.create(
                body=response_text[:1600],
                from_=settings.twilio_phone_number,
                to=from_number,
            )
        except Exception as e:
            logger.error(f"SMS send failed: {e}")

    return HTMLResponse(
        content='<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
        media_type="application/xml"
    )


# ── WEBSOCKET DASHBOARD ───────────────────────────────────────────────────────

@app.websocket("/ws/dashboard")
async def dashboard_ws(websocket: WebSocket):
    """
    WebSocket endpoint for the real-time monitoring dashboard.
    Sends live task updates, model selections, workflow steps.
    """
    await ws_manager.connect(websocket)
    try:
        # Send initial state
        await websocket.send_json({
            "type": "connected",
            "message": "OpenOwl dashboard connected",
            "timestamp": datetime.utcnow().isoformat(),
        })

        # Keep alive — ping every 30s
        while True:
            await asyncio.sleep(30)
            await websocket.send_json({"type": "ping"})

    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)
        logger.info("Dashboard disconnected")


# ── DASHBOARD HTML ────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    """Serve the built-in minimal dashboard."""
    return HTMLResponse(content="""
<!DOCTYPE html>
<html>
<head>
  <title>OpenOwl Dashboard</title>
  <meta charset="UTF-8">
  <style>
    body { background: #04060f; color: #d4deff; font-family: monospace; padding: 20px; }
    h1 { color: #4d9fff; margin-bottom: 4px; }
    .sub { color: #4a5880; margin-bottom: 20px; }
    #log { background: #080d1c; border: 1px solid #1a2540; padding: 16px;
           height: 400px; overflow-y: auto; border-radius: 8px; }
    .event { padding: 6px 0; border-bottom: 1px solid #1a2540; font-size: 12px; }
    .event .time { color: #4a5880; margin-right: 8px; }
    .event .type { color: #4d9fff; margin-right: 8px; }
    .status { display: flex; gap: 12px; margin-bottom: 16px; flex-wrap: wrap; }
    .stat { background: #080d1c; border: 1px solid #1a2540; padding: 8px 14px; border-radius: 6px; }
    .stat-val { font-size: 18px; font-weight: bold; color: #00e5a0; }
    .stat-lbl { font-size: 10px; color: #4a5880; }
    a { color: #4d9fff; }
  </style>
</head>
<body>
  <h1>🦉 OpenOwl</h1>
  <div class="sub">Real-time Mission Control · <a href="/docs">API Docs</a> · <a href="/health">Health</a></div>
  <div class="status">
    <div class="stat"><div class="stat-val" id="msg-count">0</div><div class="stat-lbl">MESSAGES</div></div>
    <div class="stat"><div class="stat-val" id="ws-status" style="color:#f5a623">CONNECTING</div><div class="stat-lbl">WEBSOCKET</div></div>
  </div>
  <div id="log"><div class="event"><span class="time">—</span><span class="type">[SYSTEM]</span>Waiting for events...</div></div>
  <script>
    let msgCount = 0;
    const log = document.getElementById('log');
    const ws = new WebSocket(`ws://${location.host}/ws/dashboard`);
    ws.onopen = () => { document.getElementById('ws-status').textContent = 'LIVE'; document.getElementById('ws-status').style.color='#00e5a0'; };
    ws.onclose = () => { document.getElementById('ws-status').textContent = 'DISCONNECTED'; document.getElementById('ws-status').style.color='#ff5f7e'; };
    ws.onmessage = (e) => {
      const data = JSON.parse(e.data);
      if (data.type === 'ping') return;
      msgCount++;
      document.getElementById('msg-count').textContent = msgCount;
      const div = document.createElement('div');
      div.className = 'event';
      const time = new Date().toTimeString().slice(0,8);
      div.innerHTML = `<span class="time">${time}</span><span class="type">[${data.type.toUpperCase()}]</span>${JSON.stringify(data.data || data.message || '').slice(0,120)}`;
      log.insertBefore(div, log.firstChild);
      if (log.children.length > 100) log.removeChild(log.lastChild);
    };
  </script>
</body>
</html>
""")


# ── MAIN ENTRY POINT ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=settings.debug,
        log_level="info",
    )
