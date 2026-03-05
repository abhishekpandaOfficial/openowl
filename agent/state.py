"""
OpenOwl Agent State
The typed state object that persists across every node in LangGraph.
Think of it as the agent's working memory for one task.
"""
from typing import TypedDict, Annotated, Optional, Any
from enum import Enum
from datetime import datetime
import operator


class TaskType(str, Enum):
    CONVERSATION   = "conversation"
    BOOKING        = "booking"
    REMINDER       = "reminder"
    RESEARCH       = "research"
    LEARN          = "learn"
    EMAIL          = "email"
    CALENDAR       = "calendar"
    MESSAGE        = "message"        # send msg to someone else
    PAYMENT        = "payment"
    SEARCH         = "search"
    SYSTEM         = "system"
    UNKNOWN        = "unknown"


class ApprovalStatus(str, Enum):
    NOT_NEEDED  = "not_needed"
    PENDING     = "pending"
    APPROVED    = "approved"
    DENIED      = "denied"
    EXPIRED     = "expired"


class ChannelType(str, Enum):
    TELEGRAM    = "telegram"
    WHATSAPP    = "whatsapp"
    SMS         = "sms"
    PHONE_CALL  = "phone_call"
    WEB         = "web"


class Message(TypedDict):
    role: str           # "user" | "assistant" | "system" | "tool"
    content: str
    timestamp: str
    channel: str


class ApprovalRequest(TypedDict):
    action_type: str    # "payment" | "email" | "message" | "delete"
    description: str    # human-readable: "Book IndiGo flight for ₹4,625"
    details: dict       # full details of what will happen
    amount: Optional[str]
    recipient: Optional[str]
    status: ApprovalStatus
    requested_at: str
    responded_at: Optional[str]


class OwlState(TypedDict):
    # ── Identity ─────────────────────────────────────────
    user_id: str
    session_id: str
    task_id: str
    channel: ChannelType
    channel_message_id: Optional[str]     # original msg id for replies

    # ── Conversation ─────────────────────────────────────
    messages: Annotated[list[Message], operator.add]  # accumulates
    current_input: str                    # raw user message
    current_persona: str                  # active agent persona name

    # ── Intent & Classification ───────────────────────────
    task_type: TaskType
    intent_confidence: float
    intent_entities: dict                 # extracted: dates, names, amounts
    language: str                         # detected language code

    # ── Model Selection ───────────────────────────────────
    selected_model: str                   # which model was chosen
    model_provider: str                   # groq | ollama | anthropic | openai
    model_latency_ms: Optional[int]

    # ── Task Execution ────────────────────────────────────
    task_plan: list[str]                  # steps Owl will take
    tool_calls: Annotated[list[dict], operator.add]
    tool_results: Annotated[list[dict], operator.add]
    final_answer: Optional[str]           # the response to send back

    # ── Approval Guardrail ────────────────────────────────
    needs_approval: bool
    approval_request: Optional[ApprovalRequest]
    approval_status: ApprovalStatus

    # ── Memory ────────────────────────────────────────────
    user_context: dict                    # loaded user preferences/history
    conversation_history: list[Message]   # last N messages

    # ── Status & Errors ───────────────────────────────────
    status: str                           # running | done | error | waiting
    error: Optional[str]
    started_at: str
    completed_at: Optional[str]

    # ── Dashboard Streaming ───────────────────────────────
    workflow_log: Annotated[list[dict], operator.add]  # for real-time UI
