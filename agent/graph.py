"""
OpenOwl Core Agent — LangGraph StateGraph
The brain of OpenOwl. Every message flows through this graph.

Graph structure:
  receive_input → parse_intent → select_model → check_approval
                                                    ├── NO  → execute_task → update_memory → send_reply
                                                    └── YES → request_approval → [WAIT] → execute_task
"""
import json
import logging
import re
from datetime import datetime
from uuid import uuid4
from typing import Literal

from langgraph.graph import StateGraph, END, START
from langgraph.checkpoint.memory import MemorySaver

from agent.state import OwlState, TaskType, ApprovalStatus, ChannelType
from models.router import model_router, TASK_MODEL_MAP
from personas.agents import get_persona, detect_persona_switch, build_system_prompt
from memory.store import OwlMemory
from config import settings

logger = logging.getLogger(__name__)


# ── INTENT CLASSIFICATION PROMPT ─────────────────────────────────────────────

INTENT_SYSTEM = """You are an intent classifier. Given a user message, extract:
1. task_type: one of [conversation, booking, reminder, research, learn, email, calendar, message, payment, search, system, unknown]
2. entities: key info like dates, names, amounts, places
3. needs_approval: true if task involves money/sending emails/messaging others
4. language: 2-letter language code (en, hi, te, od, etc.)

Respond ONLY with valid JSON like:
{
  "task_type": "booking",
  "entities": {"destination": "Mumbai", "date": "Friday", "type": "flight"},
  "needs_approval": true,
  "language": "en",
  "confidence": 0.95
}"""


# ── NODE: receive_input ───────────────────────────────────────────────────────

async def receive_input(state: OwlState, config) -> OwlState:
    """
    First node. Loads user context and checks for persona switch.
    """
    logger.info(f"📥 Receiving input from user {state['user_id']}")

    # Load user context from memory
    # memory: OwlMemory = state.get("_memory")  # injected at graph call time
    memory: OwlMemory = config["configurable"]["memory"]
    if memory:
        user_context = await memory.load_user_context(
            state["user_id"],
            state["channel"],
            config["configurable"].get("user_name", ""),
        )
    else:
        user_context = {"persona": settings.default_persona, "preferences": {}}

    # Check if user is switching persona
    persona_switch = detect_persona_switch(state["current_input"])
    if persona_switch:
        user_context["persona"] = persona_switch
        if memory:
            await memory.postgres.update_user_persona(state["user_id"], persona_switch)

    persona = get_persona(user_context.get("persona", settings.default_persona))

    return {
        **state,
        "user_context": user_context,
        "current_persona": persona.id,
        "status": "running",
        "workflow_log": [{
            "node": "receive_input",
            "status": "done",
            "message": f"Input received via {state['channel']}",
            "timestamp": datetime.utcnow().isoformat(),
        }],
    }


# ── NODE: parse_intent ────────────────────────────────────────────────────────

async def parse_intent(state: OwlState) -> OwlState:
    """
    Classify the user's intent and extract entities.
    Uses small/fast model — this should be sub-200ms.
    """
    logger.info("🧠 Parsing intent...")

    messages = [{"role": "user", "content": state["current_input"]}]

    try:
        response_text, model_used, latency = await model_router.complete(
            messages=messages,
            task_type="conversation",
            system_prompt=INTENT_SYSTEM,
        )

        # Parse JSON response
        # Strip any markdown fences if present
        clean = re.sub(r"```json|```", "", response_text).strip()
        intent_data = json.loads(clean)

        task_type = TaskType(intent_data.get("task_type", "unknown"))
        entities = intent_data.get("entities", {})
        needs_approval = intent_data.get("needs_approval", False)
        language = intent_data.get("language", "en")
        confidence = intent_data.get("confidence", 0.8)

    except (json.JSONDecodeError, ValueError) as e:
        logger.warning(f"Intent parse failed ({e}), defaulting to conversation")
        task_type = TaskType.CONVERSATION
        entities = {}
        needs_approval = False
        language = "en"
        confidence = 0.5
        model_used = "none"
        latency = 0

    # Override: certain task types ALWAYS need approval regardless of model response
    if task_type in [TaskType.PAYMENT, TaskType.EMAIL, TaskType.MESSAGE]:
        needs_approval = True

    logger.info(f"Intent: {task_type} | Approval needed: {needs_approval}")

    return {
        **state,
        "task_type": task_type,
        "intent_entities": entities,
        "needs_approval": needs_approval,
        "language": language,
        "intent_confidence": confidence,
        "workflow_log": [{
            "node": "parse_intent",
            "status": "done",
            "message": f"Intent: {task_type} | Confidence: {confidence:.0%}",
            "timestamp": datetime.utcnow().isoformat(),
        }],
    }


# ── NODE: select_model ────────────────────────────────────────────────────────

async def select_model(state: OwlState) -> OwlState:
    """
    Decide which AI model to use for this task.
    OSS models first, cloud only as fallback.
    """
    task_type = state["task_type"].value
    size = TASK_MODEL_MAP.get(task_type, "medium")

    # Map size to model string for display
    if settings.groq_api_key:
        if size == "small":
            model_display = f"groq/{settings.groq_small_model}"
        elif size == "large":
            model_display = f"groq/{settings.groq_large_model}"
        else:
            model_display = f"groq/{settings.groq_primary_model}"
        provider = "groq"
    elif settings.ollama_enabled:
        model_display = f"ollama/{settings.ollama_model}"
        provider = "ollama"
    elif settings.anthropic_api_key:
        model_display = f"anthropic/{settings.claude_model}"
        provider = "anthropic"
    else:
        model_display = f"openai/{settings.openai_model}"
        provider = "openai"

    logger.info(f"🤖 Selected model: {model_display}")

    return {
        **state,
        "selected_model": model_display,
        "model_provider": provider,
        "workflow_log": [{
            "node": "select_model",
            "status": "done",
            "message": f"Model: {model_display}",
            "timestamp": datetime.utcnow().isoformat(),
        }],
    }


# ── NODE: check_approval ──────────────────────────────────────────────────────

async def check_approval(state: OwlState, config) -> OwlState:
    """Evaluate if approval is required. Sets up the approval request if so."""
    if not state["needs_approval"]:
        return {
            **state,
            "approval_status": ApprovalStatus.NOT_NEEDED,
            "workflow_log": [{
                "node": "check_approval",
                "status": "done",
                "message": "No approval required",
                "timestamp": datetime.utcnow().isoformat(),
            }],
        }

    # Build approval request
    task_type = state["task_type"]
    entities = state["intent_entities"]

    # Human-readable description
    descriptions = {
        TaskType.PAYMENT: f"Process payment: {entities.get('amount', '?')} to {entities.get('recipient', '?')}",
        TaskType.EMAIL: f"Send email to {entities.get('to', '?')}: {entities.get('subject', 'no subject')}",
        TaskType.MESSAGE: f"Send message to {entities.get('recipient', '?')}: {entities.get('content', state['current_input'][:80])}",
        TaskType.BOOKING: f"Book {entities.get('type', 'item')}: {entities.get('destination', '')} {entities.get('date', '')} — est. {entities.get('cost', '?')}",
    }
    description = descriptions.get(task_type, f"Execute: {state['current_input'][:100]}")

    approval_request = {
        "action_type": task_type.value,
        "description": description,
        "details": entities,
        "amount": entities.get("amount") or entities.get("cost"),
        "recipient": entities.get("recipient") or entities.get("to"),
        "status": ApprovalStatus.PENDING,
        "requested_at": datetime.utcnow().isoformat(),
        "responded_at": None,
        "task_id": state["task_id"],
    }

    # Store in Redis so the approval handler can find it
    memory: OwlMemory = config["configurable"]["memory"]
    if memory:
        await memory.redis.store_pending_approval(state["task_id"], approval_request)

    return {
        **state,
        "approval_status": ApprovalStatus.PENDING,
        "approval_request": approval_request,
        "workflow_log": [{
            "node": "check_approval",
            "status": "waiting",
            "message": f"Waiting for approval: {description}",
            "timestamp": datetime.utcnow().isoformat(),
        }],
    }


# ── NODE: request_approval ────────────────────────────────────────────────────

async def request_approval(state: OwlState) -> OwlState:
    """
    Sends the approval request to the user.
    LangGraph will INTERRUPT here and wait for the user to respond YES/NO.
    """
    persona = get_persona(state["current_persona"])
    req = state["approval_request"]

    # Build the approval message in persona's style
    approval_msg = f"""
{persona.approval_style}

*{req['description']}*

"""
    if req.get("amount"):
        approval_msg += f"💰 Amount: *{req['amount']}*\n"
    if req.get("recipient"):
        approval_msg += f"👤 To: *{req['recipient']}*\n"

    approval_msg += f"""
Task ID: `{state['task_id'][:8]}`

Reply *YES* to confirm or *NO* to cancel.
_(This request expires in 5 minutes)_
""".strip()

    return {
        **state,
        "final_answer": approval_msg,   # this gets sent back to user
        "status": "waiting_approval",
        "workflow_log": [{
            "node": "request_approval",
            "status": "waiting",
            "message": "Approval request sent to user",
            "timestamp": datetime.utcnow().isoformat(),
        }],
    }


# ── NODE: execute_task ────────────────────────────────────────────────────────

async def execute_task(state: OwlState, config) -> OwlState:
    """
    Main execution node. Calls the AI model with full context and persona.
    This is where the actual response is generated.
    """
    logger.info(f"⚡ Executing task: {state['task_type']}")

    user_context = state.get("user_context", {})
    user_name = user_context.get("name") or "there"

    # Build persona system prompt
    persona = get_persona(state["current_persona"])
    system_prompt = build_system_prompt(persona, user_name, user_context)

    # Build conversation messages
    # history = state.get("conversation_history", [])
    # history = await memory.redis.get_history(state["user_id"])
    memory: OwlMemory = config["configurable"]["memory"]
    history = await memory.redis.get_history(state["user_id"]) or []
    
    messages = []

    # Add recent history for context
    for msg in history[-settings.context_window:]:
        messages.append({
            "role": msg["role"],
            "content": msg["content"],
        })

    # Add current user message
    messages.append({
        "role": "user",
        "content": state["current_input"],
    })

    # Add any tool results as context
    if state.get("tool_results"):
        tool_context = "\n\nTool results available:\n"
        for result in state["tool_results"]:
            tool_context += f"- {result.get('tool')}: {result.get('result', '')[:200]}\n"
        # Append to last user message
        messages[-1]["content"] += tool_context

    try:
        response_text, model_used, latency = await model_router.complete(
            messages=messages,
            task_type=state["task_type"].value,
            system_prompt=system_prompt,
        )

        logger.info(f"✅ Task complete | model={model_used} | {latency}ms")

        return {
            **state,
            "final_answer": response_text,
            "selected_model": model_used,
            "model_latency_ms": latency,
            "status": "done",
            "workflow_log": [{
                "node": "execute_task",
                "status": "done",
                "message": f"Response generated | {model_used} | {latency}ms",
                "timestamp": datetime.utcnow().isoformat(),
            }],
        }

    except Exception as e:
        logger.error(f"❌ Task execution failed: {e}")
        persona = get_persona(state["current_persona"])
        error_msg = {
            "aria":  "Hit an issue executing that. Want me to try a different approach?",
            "priya": "Oops, something went wrong on my end 😅 Should I try again?",
            "nova":  "oof something broke ngl. try again?",
            "meera": "Kuch technical problem aa gayi 🙏 Dobara try karein?",
            "zara":  "I encountered an error. Shall I attempt an alternative approach?",
        }.get(state["current_persona"], "Something went wrong. Please try again.")

        return {
            **state,
            "final_answer": error_msg,
            "status": "error",
            "error": str(e),
        }


# ── NODE: update_memory ───────────────────────────────────────────────────────

async def update_memory(state: OwlState, config) -> OwlState:
    """
    After a successful task, extract and save useful memories.
    Runs async in background — doesn't block the response.
    """
    memory: OwlMemory = config["configurable"]["memory"]
    if not memory:
        return state

    try:
        await memory.save_interaction(
            user_id=state["user_id"],
            user_msg=state["current_input"],
            assistant_msg=state.get("final_answer", ""),
            task_data={
                "task_id": state["task_id"],
                "task_type": state["task_type"].value,
                "channel": state["channel"],
                "model_used": state.get("selected_model", ""),
                "latency_ms": state.get("model_latency_ms", 0),
                "status": state.get("status", "done"),
                "needs_approval": state.get("needs_approval", False),
                "approval_status": state.get("approval_status", "not_needed"),
                "tool_calls": state.get("tool_calls", []),
            }
        )
    except Exception as e:
        logger.warning(f"Memory save failed (non-critical): {e}")

    return {
        **state,
        "workflow_log": [{
            "node": "update_memory",
            "status": "done",
            "message": "Context saved to memory",
            "timestamp": datetime.utcnow().isoformat(),
        }],
    }


# ── NODE: send_reply ──────────────────────────────────────────────────────────

async def send_reply(state: OwlState) -> OwlState:
    """
    Final node. The actual channel sending is handled by the channel handler
    that called this graph. This node just marks completion and formats.
    """
    return {
        **state,
        "completed_at": datetime.utcnow().isoformat(),
        "status": "done",
        "workflow_log": [{
            "node": "send_reply",
            "status": "done",
            "message": f"Reply ready for {state['channel']}",
            "timestamp": datetime.utcnow().isoformat(),
        }],
    }


# ── CONDITIONAL EDGES ─────────────────────────────────────────────────────────

def route_after_approval_check(state: OwlState) -> Literal["request_approval", "execute_task"]:
    """Route based on whether approval is needed."""
    if state.get("needs_approval") and state.get("approval_status") == ApprovalStatus.PENDING:
        return "request_approval"
    return "execute_task"


def route_after_approval_response(state: OwlState) -> Literal["execute_task", "send_reply"]:
    """Route based on user's approval decision."""
    status = state.get("approval_status")
    if status == ApprovalStatus.APPROVED:
        return "execute_task"
    if status == ApprovalStatus.DENIED:
        state["final_answer"] = "❌ Task cancelled."
        # Denied, expired, or anything else — send reply with cancellation message
        return "send_reply"


# ── BUILD THE GRAPH ───────────────────────────────────────────────────────────

def build_agent_graph() -> StateGraph:
    """Construct and compile the OpenOwl LangGraph StateGraph."""

    # Checkpointer: stores graph state at each step (enables pause/resume)
    checkpointer = MemorySaver()

    graph = StateGraph(OwlState)

    # Add all nodes
    graph.add_node("receive_input",    receive_input)
    graph.add_node("parse_intent",     parse_intent)
    graph.add_node("select_model",     select_model)
    graph.add_node("check_approval",   check_approval)
    graph.add_node("request_approval", request_approval)
    graph.add_node("execute_task",     execute_task)
    graph.add_node("update_memory",    update_memory)
    graph.add_node("send_reply",       send_reply)

    # Linear flow
    graph.add_edge(START,             "receive_input")
    graph.add_edge("receive_input",   "parse_intent")
    graph.add_edge("parse_intent",    "select_model")
    graph.add_edge("select_model",    "check_approval")

    # Conditional: needs approval?
    graph.add_conditional_edges(
        "check_approval",
        route_after_approval_check,
        {
            "request_approval": "request_approval",
            "execute_task":     "execute_task",
        }
    )

    # request_approval → INTERRUPT (LangGraph waits here for user to respond)
    # After resume, route_after_approval_response decides what to do
    graph.add_conditional_edges(
        "request_approval",
        route_after_approval_response,
        {
            "execute_task": "execute_task",
            "send_reply":   "send_reply",
        }
    )

    # After execution
    graph.add_edge("execute_task",    "update_memory")
    graph.add_edge("update_memory",   "send_reply")
    graph.add_edge("send_reply",      END)

    # ── INTERRUPT BEFORE request_approval ────────────────────────────────────
    # This is the key LangGraph feature: the graph PAUSES here and waits
    # for an external event (user replying YES/NO) before continuing.
    compiled = graph.compile(
        checkpointer=checkpointer,
        interrupt_before=["request_approval"],
    )

    logger.info("✅ LangGraph agent compiled successfully")
    return compiled


# Singleton — compiled once at startup
owl_graph = build_agent_graph()


# ── PUBLIC API ────────────────────────────────────────────────────────────────

async def run_agent(
    user_id: str,
    message: str,
    channel: str,
    memory: OwlMemory,
    session_id: str = None,
    user_name: str = "",
    channel_message_id: str = None,
) -> dict:
    """
    Main entry point. Run the agent for a user message.
    Returns dict with: response, model_used, task_id, needs_approval, status
    """
    task_id = str(uuid4())
    session_id = session_id or f"{user_id}:{channel}"

    initial_state: OwlState = {
        "user_id": user_id,
        "session_id": session_id,
        "task_id": task_id,
        # "channel": channel,
        "channel": ChannelType(channel.lower()),
        "channel_message_id": channel_message_id,
        "messages": [],
        "current_input": message,
        "current_persona": settings.default_persona,
        "task_type": TaskType.UNKNOWN,
        "intent_confidence": 0.0,
        "intent_entities": {},
        "language": "en",
        "selected_model": "",
        "model_provider": "",
        "model_latency_ms": None,
        "task_plan": [],
        "tool_calls": [],
        "tool_results": [],
        "final_answer": None,
        "needs_approval": False,
        "approval_request": None,
        "approval_status": ApprovalStatus.NOT_NEEDED,
        "user_context": {},
        "conversation_history": [],
        "status": "pending",
        "error": None,
        "started_at": datetime.utcnow().isoformat(),
        "completed_at": None,
        "workflow_log": [],
        # Private: injected context (not part of TypedDict contract)
        # "_memory": memory,
        # "_user_name": user_name,
    }

    # config = {"configurable": {"thread_id": session_id}}
    config = {
    "configurable": {
        "thread_id": session_id,
        "memory": memory,
        "user_name": user_name
    }
}

    # Run the graph
    final_state = await owl_graph.ainvoke(initial_state, config=config)

    return {
        "task_id": task_id,
        "response": final_state.get("final_answer", ""),
        "model_used": final_state.get("selected_model", ""),
        "latency_ms": final_state.get("model_latency_ms"),
        "needs_approval": final_state.get("needs_approval", False),
        "approval_status": final_state.get("approval_status"),
        "status": final_state.get("status", "done"),
        "workflow_log": final_state.get("workflow_log", []),
    }


async def resume_after_approval(
    session_id: str,
    task_id: str,
    approved: bool,
    memory: OwlMemory,
) -> dict:
    """
    Resume a paused graph after the user responds YES/NO to an approval.
    This is the LangGraph 'human-in-the-loop' resume mechanism.
    """

    # 🔐 Check if approval still exists
    approval = await memory.redis.get_pending_approval(task_id)

    if not approval:
        return {
            "task_id": task_id,
            "response": "⏰ This approval request has expired. Please start the task again.",
            "status": "expired"
        }
       #  Mark approval as resolved
    await memory.redis.resolve_approval(task_id, approved, "user")

    if not approved:
        return {
        "task_id": task_id,
        "response": "❌ Task cancelled.",
        "status": "cancelled"
    }
    # config = {"configurable": {"thread_id": session_id}}
    config = {
    "configurable": {
        "thread_id": session_id,
        "memory": memory
    }
}

    # Update the approval status in the current state
    new_approval_status = ApprovalStatus.APPROVED if approved else ApprovalStatus.DENIED

    # Inject the approval decision and resume
    await owl_graph.aupdate_state(
        config,
        {"approval_status": new_approval_status},
        as_node="request_approval",
    )

    # Continue the graph from where it paused
    final_state = await owl_graph.ainvoke(None, config=config)

    return {
        "task_id": task_id,
        "response": final_state.get("final_answer", ""),
        "status": final_state.get("status", "done"),
        "model_used": final_state.get("selected_model", ""),
        "workflow_log": final_state.get("workflow_log", []),
    }
