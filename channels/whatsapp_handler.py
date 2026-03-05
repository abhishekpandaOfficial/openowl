"""
OpenOwl WhatsApp Channel Handler (Phase 2 — Full Integration)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Handles:
  • Inbound text messages  → LangGraph agent
  • Inbound voice notes    → Whisper STT → agent
  • Inbound images         → Vision model description → agent
  • Outbound rich messages → text / lists / buttons
  • Approval YES/NO flow   → resume paused LangGraph task
  • Two-way sync status    → real-time via WebSocket dashboard
  • Delivery status hooks  → track sent/delivered/read
"""
import logging
import re
import tempfile
import os
from typing import Optional
from datetime import datetime
from urllib.parse import parse_qs

import httpx
from fastapi import Request, HTTPException
from fastapi.responses import HTMLResponse

from agent.graph import run_agent, resume_after_approval
from memory.store import OwlMemory
from config import settings

logger = logging.getLogger(__name__)

# ── MESSAGE TEMPLATES ─────────────────────────────────────────────────────────
# WhatsApp supports limited formatting: *bold*, _italic_, ~strike~, ```code```

APPROVAL_TEMPLATE = """{approval_style}

*Action:* {description}
{amount_line}{recipient_line}
─────────────────────
Reply *YES* to confirm ✅
Reply *NO* to cancel ❌

_(Expires in 5 minutes · Task ID: {short_id})_"""

MORNING_BRIEF_TEMPLATE = """🌅 *Good {time_of_day}, {name}!*

Here's your OpenOwl briefing for {date}:

📅 *Today's meetings:*
{meetings}

📧 *Unread emails:* {email_count} new
{email_preview}

🌤️ *Weather in {city}:* {weather}

✅ *Pending tasks:* {task_count}
{tasks_preview}

_Reply with any task and I'll handle it!_"""


class WhatsAppHandler:
    """
    Full WhatsApp integration via Twilio.
    Handles inbound messages, sends rich replies, tracks delivery.
    """

    def __init__(self, memory: OwlMemory, ws_broadcast_fn=None):
        self.memory = memory
        self.broadcast = ws_broadcast_fn  # WebSocket dashboard broadcast
        self._twilio_client = None

    def _get_twilio(self):
        """Lazy-init Twilio client."""
        if not self._twilio_client:
            if not settings.twilio_account_sid:
                raise ValueError(
                    "TWILIO_ACCOUNT_SID not set in .env\n"
                    "Sign up at twilio.com (free $15 trial)"
                )
            from twilio.rest import Client
            self._twilio_client = Client(
                settings.twilio_account_sid,
                settings.twilio_auth_token
            )
        return self._twilio_client

    # ── INBOUND MESSAGE ROUTER ────────────────────────────────────────────────

    async def handle_inbound(self, request: Request) -> HTMLResponse:
        """
        Main inbound webhook from Twilio.
        Called for every WhatsApp message sent to your number.
        """
        body = await request.body()
        params = parse_qs(body.decode("utf-8"))

        def p(key): return (params.get(key) or [""])[0]

        from_raw    = p("From")          # "whatsapp:+919876543210"
        from_number = from_raw.replace("whatsapp:", "")
        body_text   = p("Body")
        msg_sid     = p("MessageSid")
        media_count = int(p("NumMedia") or "0")
        profile_name = p("ProfileName")
        media_url    = p("MediaUrl0") if media_count > 0 else ""
        media_type   = p("MediaContentType0") if media_count > 0 else ""

        if not from_number:
            return self._twiml_empty()

        logger.info(f"📱 WhatsApp IN | {from_number} | {body_text[:60]}")

        # Rate limit
        allowed = await self.memory.redis.check_rate_limit(
            f"wa:{from_number}", limit=30, window=60
        )
        if not allowed:
            await self.send_text(from_number,
                "⏳ You're sending too fast! I can handle 30 messages per minute.")
            return self._twiml_empty()

        # ── Approval YES/NO intercept ─────────────────────────────────────
        active_task = await self.memory.redis.get_active_task(f"wa:{from_number}")
        if active_task and body_text.strip().upper() in ["YES", "NO", "Y", "N"]:
            approved = body_text.strip().upper() in ["YES", "Y"]
            await self._handle_approval_response(from_number, active_task, approved)
            return self._twiml_empty()

        # ── Voice note ────────────────────────────────────────────────────
        if media_url and media_type and "audio" in media_type:
            body_text = await self._transcribe_voice_note(media_url, from_number)
            if not body_text:
                await self.send_text(from_number,
                    "🎤 Sorry, I couldn't understand that voice note. "
                    "Please try again or send a text message.")
                return self._twiml_empty()
            await self.send_text(from_number, f"🎤 _Heard: {body_text}_")

        # ── Image ─────────────────────────────────────────────────────────
        elif media_url and media_type and "image" in media_type:
            body_text = await self._describe_image(media_url, body_text)

        # ── No text or media ──────────────────────────────────────────────
        if not body_text:
            await self.send_text(from_number,
                "👋 Hi! Send me a message and I'll help you out.")
            return self._twiml_empty()

        # ── Run through LangGraph agent ───────────────────────────────────
        try:
            result = await run_agent(
                user_id=f"wa:{from_number}",
                message=body_text,
                channel="whatsapp",
                memory=self.memory,
                session_id=f"wa:{from_number}",
                user_name=profile_name,
                channel_message_id=msg_sid,
            )

            response_text = result.get("response", "")
            needs_approval = result.get("needs_approval", False)
            approval_status = result.get("approval_status", "not_needed")

            if needs_approval and approval_status == "pending":
                # Store active task for YES/NO intercept
                await self.memory.redis.set_active_task(
                    f"wa:{from_number}", result["task_id"]
                )
                await self.send_text(from_number, response_text)
            else:
                await self.send_text(from_number, response_text)

            # Broadcast to dashboard
            if self.broadcast:
                await self.broadcast({
                    "type": "whatsapp_message",
                    "direction": "in",
                    "channel": "whatsapp",
                    "from": from_number,
                    "preview": body_text[:80],
                    "model": result.get("model_used", ""),
                    "latency": result.get("latency_ms", 0),
                    "task_type": result.get("workflow_log", [{}])[-1]
                        .get("message", "") if result.get("workflow_log") else "",
                    "timestamp": datetime.utcnow().isoformat(),
                })

        except Exception as e:
            logger.error(f"WhatsApp agent error: {e}", exc_info=True)
            await self.send_text(from_number,
                "⚠️ Something went wrong on my end. Please try again in a moment."
            )

        return self._twiml_empty()

    # ── APPROVAL HANDLING ─────────────────────────────────────────────────────

    async def _handle_approval_response(
        self, phone: str, task_id: str, approved: bool
    ):
        """Resume the paused LangGraph task after user says YES/NO."""
        await self.memory.redis.resolve_approval(task_id, approved, f"wa:{phone}")
        await self.memory.redis.clear_active_task(f"wa:{phone}")

        if approved:
            await self.send_text(phone, "✅ *Approved!* Processing now...")
            try:
                result = await resume_after_approval(
                    session_id=f"wa:{phone}",
                    task_id=task_id,
                    approved=True,
                    memory=self.memory,
                )
                await self.send_text(phone, result.get("response", "✅ Done!"))
            except Exception as e:
                logger.error(f"Resume after WA approval failed: {e}")
                await self.send_text(phone,
                    "⚠️ Error resuming task. Please try again.")
        else:
            await self.send_text(phone, "❌ *Cancelled.* No action was taken.")

    # ── SEND METHODS ──────────────────────────────────────────────────────────

    async def send_text(self, to_number: str, message: str):
        """
        Send a WhatsApp text message.
        Splits messages longer than 1600 chars automatically.
        """
        if not settings.twilio_account_sid:
            logger.info(f"[WhatsApp MOCK] → {to_number}: {message[:100]}")
            return

        client = self._get_twilio()
        clean_to = to_number.replace("whatsapp:", "")

        # Split if too long
        chunks = [message[i:i+1600] for i in range(0, len(message), 1600)]
        for chunk in chunks:
            try:
                msg = client.messages.create(
                    body=chunk,
                    from_=settings.twilio_whatsapp_number,
                    to=f"whatsapp:{clean_to}",
                )
                logger.info(f"📤 WhatsApp OUT | {clean_to} | SID: {msg.sid}")

                # Broadcast to dashboard
                if self.broadcast:
                    await self.broadcast({
                        "type": "whatsapp_message",
                        "direction": "out",
                        "channel": "whatsapp",
                        "to": clean_to,
                        "preview": chunk[:80],
                        "timestamp": datetime.utcnow().isoformat(),
                    })

            except Exception as e:
                logger.error(f"WhatsApp send failed to {clean_to}: {e}")

    async def send_approval_request(
        self,
        to_number: str,
        description: str,
        task_id: str,
        amount: str = "",
        recipient: str = "",
        approval_style: str = "Here's what I'm about to do — confirm to proceed:",
    ):
        """Send a formatted approval request message."""
        amount_line   = f"💰 *Amount:* {amount}\n" if amount else ""
        recipient_line = f"👤 *To:* {recipient}\n" if recipient else ""

        msg = APPROVAL_TEMPLATE.format(
            approval_style=approval_style,
            description=description,
            amount_line=amount_line,
            recipient_line=recipient_line,
            short_id=task_id[:8],
        )
        await self.send_text(to_number, msg)

    async def send_proactive(self, to_number: str, message: str):
        """
        Send an unsolicited message to the user (reminders, briefings).
        Used by the scheduler for morning briefings and reminders.
        """
        logger.info(f"📢 Proactive WhatsApp → {to_number}")
        await self.send_text(to_number, message)

    # ── MEDIA HANDLING ────────────────────────────────────────────────────────

    async def _transcribe_voice_note(
        self, media_url: str, user_id: str
    ) -> Optional[str]:
        """Download and transcribe a WhatsApp voice note using Whisper."""
        try:
            # Download audio from Twilio
            auth = (settings.twilio_account_sid, settings.twilio_auth_token)
            async with httpx.AsyncClient(auth=auth, timeout=30) as client:
                resp = await client.get(media_url)
                resp.raise_for_status()
                audio_bytes = resp.content

            # Save to temp file
            with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
                f.write(audio_bytes)
                temp_path = f.name

            # Transcribe with local Whisper
            try:
                import whisper
                model = whisper.load_model("base")
                result = model.transcribe(temp_path)
                text = result["text"].strip()
            except ImportError:
                # Fallback to OpenAI API whisper
                if settings.openai_api_key:
                    from openai import OpenAI
                    oai = OpenAI(api_key=settings.openai_api_key)
                    with open(temp_path, "rb") as audio_file:
                        transcript = oai.audio.transcriptions.create(
                            model="whisper-1", file=audio_file
                        )
                    text = transcript.text
                else:
                    logger.warning("Whisper not available. "
                                   "Install: pip install openai-whisper")
                    return None
            finally:
                os.unlink(temp_path)

            logger.info(f"🎤 Transcribed: {text[:80]}")
            return text

        except Exception as e:
            logger.error(f"Voice transcription failed: {e}")
            return None

    async def _describe_image(
        self, media_url: str, caption: str = ""
    ) -> str:
        """Use vision model to describe an image the user sent."""
        prompt = f"User sent an image"
        if caption:
            prompt += f" with caption: '{caption}'"
        prompt += ". Describe what's in it briefly and respond helpfully."

        # If OpenAI with vision is available
        if settings.openai_api_key:
            try:
                from openai import OpenAI
                client = OpenAI(api_key=settings.openai_api_key)
                response = client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[{
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {"url": media_url}},
                        ],
                    }],
                    max_tokens=300,
                )
                return response.choices[0].message.content
            except Exception as e:
                logger.warning(f"Vision description failed: {e}")

        # Fallback: just use caption
        return caption or "User sent an image."

    # ── DELIVERY STATUS WEBHOOK ───────────────────────────────────────────────

    async def handle_status_callback(self, request: Request):
        """
        Handle Twilio delivery status callbacks.
        Updates dashboard with sent/delivered/read status.
        """
        body = await request.body()
        params = parse_qs(body.decode())
        def p(k): return (params.get(k) or [""])[0]

        msg_sid = p("MessageSid")
        status  = p("MessageStatus")   # sent | delivered | read | failed
        to      = p("To").replace("whatsapp:", "")

        logger.info(f"📊 WhatsApp status | {msg_sid} | {status} → {to}")

        if self.broadcast:
            await self.broadcast({
                "type": "whatsapp_status",
                "message_sid": msg_sid,
                "status": status,
                "to": to,
                "timestamp": datetime.utcnow().isoformat(),
            })

        return {"ok": True}

    # ── HELPERS ───────────────────────────────────────────────────────────────

    @staticmethod
    def _twiml_empty() -> HTMLResponse:
        """Empty TwiML response — tells Twilio we handled the message."""
        return HTMLResponse(
            content='<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
            media_type="application/xml",
        )
