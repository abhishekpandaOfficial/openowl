# 🦉 OpenOwl — Personal Autonomous Agent

> Talk to it like a human. It listens, understands, executes — and reports back.  
> WhatsApp · Telegram · SMS · Phone Call · Two-way always.

[![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](https://www.gnu.org/licenses/agpl-3.0)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-green.svg)](https://python.org)
[![LangGraph](https://img.shields.io/badge/orchestration-LangGraph-purple.svg)](https://langchain-ai.github.io/langgraph/)

---

## What is OpenOwl?

OpenOwl is an advanced personal AI assistant that lives inside your WhatsApp and Telegram. Unlike ChatGPT (which needs an app) or Siri (which is dumb), OpenOwl:

- **Runs autonomously** — give it a multi-step task and it handles everything
- **Always two-way** — every message gets a real response on the same channel
- **OSS-first** — uses free open source models (Groq/Ollama) before ever spending money
- **Safe by design** — hardcoded approval gates for money, emails, and messages to others
- **Remembers you** — learns your preferences, habits, and history across conversations

---

## ⚡ Quick Start (10 minutes)

### Prerequisites
- Docker + Docker Compose
- A Telegram account
- A Groq API key (free at [console.groq.com](https://console.groq.com))
- ngrok (for local development)

### Steps

```bash
# 1. Clone
git clone https://github.com/yourname/openowl
cd openowl

# 2. Configure
cp .env.example .env
# Edit .env — fill in TELEGRAM_BOT_TOKEN and GROQ_API_KEY (minimum)

# 3. Start everything
chmod +x scripts/start.sh
./scripts/start.sh

# 4. Expose to internet (new terminal)
ngrok http 8000
# Copy the https URL → set TELEGRAM_WEBHOOK_URL=<url> in .env
# Then: docker-compose restart openowl

# 5. Message your Telegram bot!
# Say: "Hello!" or "What can you do?"
```

### Get your Telegram bot token
1. Open Telegram → search `@BotFather`
2. Send `/newbot`
3. Choose a name (e.g. "My OpenOwl")
4. Copy the token → paste into `.env` as `TELEGRAM_BOT_TOKEN`

### Get your Groq API key (free)
1. Go to [console.groq.com](https://console.groq.com)
2. Sign up (free, no credit card)
3. Create API key → paste into `.env` as `GROQ_API_KEY`

---

## 🏗️ Architecture

```
User (WhatsApp/Telegram/SMS/Call)
         ↓
   FastAPI Server
         ↓
   LangGraph StateGraph
         ↓
   ┌─────────────────────────────────────┐
   │  receive_input → parse_intent        │
   │       → select_model                │
   │       → check_approval              │
   │            ├── NO → execute_task    │
   │            └── YES → [PAUSE]        │
   │                 ↓ (user says YES)   │
   │            execute_task             │
   │       → update_memory               │
   │       → send_reply                  │
   └─────────────────────────────────────┘
         ↓
   OSS Model Chain:
   Groq (free) → Ollama (local) → Claude Haiku → GPT-4o-mini
```

---

## 🤖 AI Model Priority

OpenOwl NEVER spends money on AI until free options are exhausted:

| Priority | Model | Cost | Speed |
|----------|-------|------|-------|
| 1 | Groq / Mistral-7B | **FREE** (6k req/day) | 300 tok/sec |
| 2 | Ollama / local | **FREE** (no internet) | Depends on GPU |
| 3 | Claude Haiku | ~₹0.02/msg | Fast |
| 4 | GPT-4o-mini | ~₹0.01/msg | Fast |

---

## 👩 Personas

Switch any time by saying "Switch to Priya" or `/switch priya`:

| Persona | Style | Best for |
|---------|-------|----------|
| **Aria** | Professional, direct | Work tasks, bookings |
| **Priya** | Warm, patient teacher | Learning, explanations |
| **Nova** | Casual Gen-Z | Quick tasks, tech |
| **Meera** | Hindi/caring | Hindi speakers, health |
| **Zara** | Formal, structured | Reports, briefings |

---

## 🔒 Approval Guardrails

These are **hardcoded** — not just prompt instructions. The LangGraph graph literally cannot proceed without your explicit YES:

- 💸 Any payment or money transaction
- 📧 Sending emails to other people
- 💬 Sending WhatsApp/SMS to other people
- 🗑️ Deleting files, emails, or events
- 📝 Publishing or posting anything

---

## 💬 Telegram Commands

```
/start    — Welcome & setup
/help     — List all commands
/switch   — Change persona (aria/priya/nova/meera/zara)
/status   — System health check
/memory   — See what Owl remembers about you
/forget   — Clear memory
```

---

## 💰 Cost Breakdown

### Local development: ₹0/month
- Everything runs on your machine
- Groq free tier handles all AI

### Production VPS: ~₹500-700/month
- Hetzner CX11: €3.79/mo
- Twilio WhatsApp number: $1/mo
- AI: ~₹10/mo (mostly free Groq)

---

## 📁 Project Structure

```
openowl/
├── main.py                  # FastAPI server (entry point)
├── config.py                # All settings from .env
├── agent/
│   ├── graph.py             # LangGraph StateGraph (the brain)
│   └── state.py             # TypedDict state definition
├── channels/
│   └── telegram_handler.py  # Telegram bot
├── memory/
│   └── store.py             # Redis + PostgreSQL memory
├── models/
│   └── router.py            # OSS-first model router
├── personas/
│   └── agents.py            # 5 human agent personas
├── scripts/
│   └── start.sh             # Quick start script
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
└── .env.example
```

---

## 🔌 Adding Integrations

OpenOwl uses a tool plugin system. Add a new integration by creating a tool:

```python
# tools/my_tool.py
from langchain_core.tools import tool

@tool
def check_weather(city: str) -> str:
    """Get current weather for a city."""
    # Your implementation
    return f"Weather in {city}: 28°C, sunny"
```

Then register it in `agent/graph.py` in the tools list.

---

## 📜 License

- **Core engine**: GNU AGPL v3 — open source forever
- **Personas & plugins**: MIT — build anything
- **Guardrails**: Non-removable (AGPL requirement)
- **"OpenOwl" name**: Trademark protected

---

## 🤝 Contributing

PRs welcome! Especially for:
- New tool integrations (Google Calendar, Gmail, Maps)
- New personas
- WhatsApp / SMS improvements
- Voice (Whisper) improvements
- Hindi/regional language support

---

*Built with ❤️ using FastAPI + LangGraph + Groq + Telegram*
