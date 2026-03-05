"""
OpenOwl Telegram Channel Handler
Handles all Telegram bot interactions:
- Receives messages via webhook
- Routes to the LangGraph agent
- Sends responses back (text, photos, voice)
- Handles approval YES/NO responses
- Handles /start, /help, /switch commands
"""
import logging
import re
from typing import Optional

from telegram import Update, Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode, ChatAction
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes
)

from agent.graph import run_agent, resume_after_approval
from memory.store import OwlMemory
from config import settings

logger = logging.getLogger(__name__)

# ── APPROVAL KEYBOARD ─────────────────────────────────────────────────────────

def approval_keyboard(task_id: str) -> InlineKeyboardMarkup:
    """Inline keyboard for approval requests."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ YES — Go ahead", callback_data=f"approve:{task_id}"),
            InlineKeyboardButton("❌ NO — Cancel",    callback_data=f"deny:{task_id}"),
        ]
    ])


# ── COMMAND HANDLERS ──────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command — onboard new users."""
    user = update.effective_user
    memory: OwlMemory = context.bot_data["memory"]

    # Create user in database
    await memory.postgres.get_or_create_user(
        user_id=str(user.id),
        channel="telegram",
        name=user.first_name or "",
    )

    persona_name = settings.default_persona.capitalize()
    welcome = f"""🦉 *Welcome to OpenOwl!*

I'm your personal autonomous assistant — *{persona_name}* is here to help.

I can:
✈️ Book flights, hotels, cabs
📅 Manage your calendar
📧 Read and draft emails
🔔 Set reminders
📚 Explain any topic in simple English
🔍 Research anything for you
💬 And much more...

*I always ask before spending money or sending messages to others.*

Just talk to me naturally — no commands needed!

_Try: "What's the weather tomorrow?" or "Remind me to call mom at 6pm"_
"""
    await update.message.reply_text(welcome, parse_mode=ParseMode.MARKDOWN)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /help command."""
    help_text = """🦉 *OpenOwl Help*

*Personas — switch anytime:*
• `/switch aria` — Professional PA (default)
• `/switch priya` — Patient teacher
• `/switch nova` — Casual, fast
• `/switch meera` — Hindi/caring
• `/switch zara` — Formal briefer

*Commands:*
• `/start` — Restart & onboarding
• `/help` — This message
• `/status` — See system status
• `/memory` — See what I remember about you
• `/forget` — Clear my memory

*Approval system:*
When I need to spend money or send a message to someone else, I'll always ask you first with YES/NO buttons.

*Natural language works best:*
_"Book me a flight to Delhi next Monday morning"_
_"Explain blockchain to me simply"_
_"Set a reminder for medicine daily at 9pm"_
"""
    await update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN)


async def cmd_switch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /switch <persona> command."""
    memory: OwlMemory = context.bot_data["memory"]
    user_id = str(update.effective_user.id)

    if not context.args:
        await update.message.reply_text(
            "Usage: `/switch aria|priya|nova|meera|zara`",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    persona_id = context.args[0].lower()
    valid = ["aria", "priya", "nova", "meera", "zara"]
    if persona_id not in valid:
        await update.message.reply_text(
            f"Unknown persona. Choose from: {', '.join(valid)}"
        )
        return

    await memory.postgres.update_user_persona(user_id, persona_id)
    await memory.redis.update_session(user_id, {"persona": persona_id})

    greetings = {
        "aria":  "👩‍💼 *Aria* here. Let's get things done.",
        "priya": "👩‍🎓 *Priya* here! What would you like to learn? 😊",
        "nova":  "🧑‍💻 *nova* online. what's up?",
        "meera": "👩‍⚕️ *Meera* hoon main! Kaise help karoon aapki? 😊",
        "zara":  "👩‍⚖️ *Zara* at your service. How may I assist you today?",
    }
    await update.message.reply_text(
        greetings.get(persona_id, f"Switched to {persona_id}"),
        parse_mode=ParseMode.MARKDOWN
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """System status check."""
    memory: OwlMemory = context.bot_data["memory"]

    # Quick health checks
    redis_ok = False
    db_ok = False

    try:
        await memory.redis._client.ping()
        redis_ok = True
    except:
        pass

    try:
        await memory.postgres.get_task_history(str(update.effective_user.id), limit=1)
        db_ok = True
    except:
        pass

    model_status = "🟢 Groq (OSS)" if settings.groq_api_key else (
        "🟡 Ollama (local)" if settings.ollama_enabled else "🔴 No model configured"
    )

    status_msg = f"""🦉 *OpenOwl Status*

*AI Model:* {model_status}
*Redis Memory:* {'🟢 Connected' if redis_ok else '🔴 Disconnected'}
*Database:* {'🟢 Connected' if db_ok else '🔴 Disconnected'}
*WhatsApp:* {'🟢 Configured' if settings.twilio_account_sid else '⚪ Not configured'}
*Telegram:* 🟢 Connected (you're here!)

_Version {settings.app_version}_
"""
    await update.message.reply_text(status_msg, parse_mode=ParseMode.MARKDOWN)


async def cmd_memory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show what OpenOwl remembers about the user."""
    memory: OwlMemory = context.bot_data["memory"]
    user_id = str(update.effective_user.id)

    memories = await memory.postgres.get_user_memories(user_id)
    history = await memory.postgres.get_task_history(user_id, limit=5)

    if not memories and not history:
        await update.message.reply_text(
            "I don't have any memories about you yet — just start chatting! 😊"
        )
        return

    msg = "🧠 *What I remember about you:*\n\n"

    if memories:
        msg += "*Preferences & facts:*\n"
        for key, value in list(memories.items())[:10]:
            msg += f"• {key}: {value}\n"

    if history:
        msg += "\n*Recent tasks:*\n"
        for task in history:
            msg += f"• {task['task_type']}: _{task['input_text'][:50]}..._\n"

    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


# ── MESSAGE HANDLER ───────────────────────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Main message handler — routes every user message through the LangGraph agent.
    """
    user = update.effective_user
    user_id = str(user.id)
    message_text = update.message.text or ""
    memory: OwlMemory = context.bot_data["memory"]

    # Rate limit check
    allowed = await memory.redis.check_rate_limit(user_id, limit=30, window=60)
    if not allowed:
        await update.message.reply_text("⏳ Slow down a bit! I can handle 30 messages per minute.")
        return

    # Check for YES/NO approval responses (text-based fallback)
    active_task = await memory.redis.get_active_task(user_id)
    if active_task and message_text.strip().upper() in ["YES", "NO", "Y", "N"]:
        approved = message_text.strip().upper() in ["YES", "Y"]
        await handle_text_approval(update, context, active_task, approved, memory, user_id)
        return

    # Show typing indicator
    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id,
        action=ChatAction.TYPING
    )

    session_id = f"tg:{user_id}"

    try:
        result = await run_agent(
            user_id=user_id,
            message=message_text,
            channel="telegram",
            memory=memory,
            session_id=session_id,
            user_name=user.first_name or "",
            channel_message_id=str(update.message.message_id),
        )

        response = result.get("response", "")
        if not response:
            response = "I processed your request but had nothing to say. Try again?"

        # If task needs approval, send with inline keyboard
        if result.get("needs_approval") and result.get("approval_status") == "pending":
            task_id = result["task_id"]
            await memory.redis.set_active_task(user_id, task_id)
            await update.message.reply_text(
                response,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=approval_keyboard(task_id),
            )
        else:
            # Regular response — send in chunks if too long
            if len(response) > 4096:
                for i in range(0, len(response), 4096):
                    await update.message.reply_text(
                        response[i:i+4096],
                        parse_mode=ParseMode.MARKDOWN,
                    )
            else:
                await update.message.reply_text(
                    response,
                    parse_mode=ParseMode.MARKDOWN,
                )

    except Exception as e:
        logger.error(f"Message handling error for user {user_id}: {e}", exc_info=True)
        await update.message.reply_text(
            "⚠️ Something went wrong on my end. Please try again in a moment."
        )


async def handle_text_approval(
    update, context, task_id: str, approved: bool,
    memory: OwlMemory, user_id: str
):
    """Handle text-based YES/NO approval responses."""
    await memory.redis.resolve_approval(task_id, approved, user_id)
    await memory.redis.clear_active_task(user_id)

    if approved:
        await update.message.reply_text("✅ Approved! Processing now...")
        session_id = f"tg:{user_id}"
        result = await resume_after_approval(
            session_id=session_id,
            task_id=task_id,
            approved=True,
            memory=memory,
        )
        await update.message.reply_text(
            result.get("response", "Done!"),
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        await update.message.reply_text("❌ Cancelled. No action taken.")


# ── CALLBACK QUERY HANDLER (inline keyboard YES/NO) ───────────────────────────

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline keyboard button presses for approvals."""
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    memory: OwlMemory = context.bot_data["memory"]
    data = query.data

    if data.startswith("approve:") or data.startswith("deny:"):
        approved = data.startswith("approve:")
        task_id = data.split(":", 1)[1]

        # Update the approval in Redis
        await memory.redis.resolve_approval(task_id, approved, user_id)
        await memory.redis.clear_active_task(user_id)

        if approved:
            # Update message to show processing
            await query.edit_message_text(
                query.message.text + "\n\n✅ *Approved — processing...*",
                parse_mode=ParseMode.MARKDOWN,
            )

            # Resume the paused graph
            session_id = f"tg:{user_id}"
            try:
                result = await resume_after_approval(
                    session_id=session_id,
                    task_id=task_id,
                    approved=True,
                    memory=memory,
                )
                await context.bot.send_message(
                    chat_id=query.message.chat_id,
                    text=result.get("response", "✅ Done!"),
                    parse_mode=ParseMode.MARKDOWN,
                )
            except Exception as e:
                logger.error(f"Resume after approval failed: {e}")
                await context.bot.send_message(
                    chat_id=query.message.chat_id,
                    text="⚠️ Error resuming task. Please try again.",
                )
        else:
            await query.edit_message_text(
                query.message.text + "\n\n❌ *Cancelled.*",
                parse_mode=ParseMode.MARKDOWN,
            )


# ── VOICE MESSAGE HANDLER ─────────────────────────────────────────────────────

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handle voice messages — transcribe with Whisper, then process as text.
    """
    user_id = str(update.effective_user.id)
    memory: OwlMemory = context.bot_data["memory"]

    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id,
        action=ChatAction.TYPING
    )

    try:
        # Download voice file
        voice = update.message.voice
        file = await context.bot.get_file(voice.file_id)

        import tempfile, os
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
            await file.download_to_drive(f.name)
            temp_path = f.name

        # Transcribe with Whisper
        transcribed_text = await transcribe_audio(temp_path)
        os.unlink(temp_path)

        if not transcribed_text:
            await update.message.reply_text("Couldn't understand the audio. Please try again or send text.")
            return

        await update.message.reply_text(f"🎤 _Heard: {transcribed_text}_", parse_mode=ParseMode.MARKDOWN)

        # Process transcribed text as regular message
        update.message.text = transcribed_text
        await handle_message(update, context)

    except Exception as e:
        logger.error(f"Voice handling error: {e}")
        await update.message.reply_text(
            "Couldn't process the voice message. Please send text instead."
        )


async def transcribe_audio(file_path: str) -> Optional[str]:
    """Transcribe audio using local Whisper (free, private)."""
    try:
        import whisper
        model = whisper.load_model("base")      # ~150MB, fast
        result = model.transcribe(file_path)
        return result["text"].strip()
    except ImportError:
        # Fallback: try OpenAI Whisper API if key available
        if settings.openai_api_key:
            from openai import OpenAI
            client = OpenAI(api_key=settings.openai_api_key)
            with open(file_path, "rb") as f:
                transcript = client.audio.transcriptions.create(
                    model="whisper-1",
                    file=f,
                )
            return transcript.text
        logger.warning("Whisper not available. Install: pip install openai-whisper")
        return None


# ── APPLICATION BUILDER ───────────────────────────────────────────────────────

def create_telegram_app(memory: OwlMemory) -> Application:
    """Build and configure the Telegram bot application."""

    if not settings.telegram_bot_token:
        raise ValueError(
            "TELEGRAM_BOT_TOKEN not set in .env\n"
            "Get one from @BotFather on Telegram"
        )

    app = Application.builder().token(settings.telegram_bot_token).build()

    # Inject memory into bot_data (accessible in all handlers)
    app.bot_data["memory"] = memory

    # Register handlers
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("help",   cmd_help))
    app.add_handler(CommandHandler("switch", cmd_switch))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("memory", cmd_memory))

    # Inline keyboard callbacks (approval YES/NO)
    app.add_handler(CallbackQueryHandler(handle_callback))

    # Voice messages
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))

    # All text messages (must be last)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info(f"✅ Telegram bot configured")
    return app


async def set_telegram_webhook(app: Application, webhook_url: str):
    """Register the webhook URL with Telegram."""
    await app.bot.set_webhook(
        url=f"{webhook_url}/telegram/webhook",
        secret_token=settings.webhook_secret,
        allowed_updates=["message", "callback_query"],
    )
    info = await app.bot.get_me()
    logger.info(f"✅ Telegram webhook set → @{info.username}")
    logger.info(f"   Webhook URL: {webhook_url}/telegram/webhook")
