"""
LangGraph chatbot service for Axiyon.ai.

Flow:
  1. classify_message  → greeting/off-topic  → instant_reply (no LLM)
                       → real_estate         → agent (LLM streaming)
  2. agent             → streams OpenAI response with full conversation history

Persistence:
  - Uses PostgresSaver (langgraph-checkpoint-postgres) in production
  - Falls back to MemorySaver for SQLite / local dev
"""

from typing import Annotated, Literal

from django.conf import settings
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from typing_extensions import TypedDict

from chatbot.prompts import SYSTEM_PROMPT
from chatbot.tools import ALL_TOOLS


# ── LangGraph state ───────────────────────────────────────────────────────────

class State(TypedDict):
    messages: Annotated[list, add_messages]


# ── LLM with tools bound ──────────────────────────────────────────────────────

def _get_llm():
    return ChatOpenAI(
        model="gpt-4o-mini",
        temperature=0.4,
        streaming=True,
        openai_api_key=settings.OPENAI_API_KEY,
    ).bind_tools(ALL_TOOLS)


# ── Nodes ─────────────────────────────────────────────────────────────────────

def _sanitize_messages(messages: list) -> list:
    """
    Drop dangling tool calls so OpenAI doesn't reject the request.

    A prior run may have persisted an AIMessage with `tool_calls` but crashed
    before the matching ToolMessage responses were saved (e.g. an API error
    mid-tool-loop). On resume, OpenAI rejects the history with:
      "An assistant message with 'tool_calls' must be followed by tool messages
       responding to each 'tool_call_id'."
    Here we strip the tool_calls from any AIMessage whose tool_call_ids are not
    all answered by a following ToolMessage, and drop orphan ToolMessages.
    """
    # tool_call_ids that have a ToolMessage response.
    answered_ids = {
        m.tool_call_id
        for m in messages
        if isinstance(m, ToolMessage) and getattr(m, "tool_call_id", None)
    }
    # tool_call_ids that survive (i.e. their AIMessage keeps the call).
    kept_call_ids: set[str] = set()

    cleaned = []
    for msg in messages:
        if isinstance(msg, AIMessage) and getattr(msg, "tool_calls", None):
            answered = [tc for tc in msg.tool_calls if tc.get("id") in answered_ids]
            if len(answered) != len(msg.tool_calls):
                # Some tool_calls are unanswered — strip them all to keep the
                # assistant turn valid. Keep the text content if any.
                if (msg.content or "").strip():
                    cleaned.append(AIMessage(content=msg.content))
                # Drop the dangling tool_calls entirely.
                continue
            kept_call_ids.update(tc.get("id") for tc in answered)
        elif isinstance(msg, ToolMessage):
            # Drop orphan ToolMessages whose AIMessage tool_call was removed.
            if getattr(msg, "tool_call_id", None) not in kept_call_ids:
                continue
        cleaned.append(msg)
    return cleaned


def agent_node(state: State) -> State:
    """Call LLM with tools. LLM decides whether to call a tool or respond directly."""
    from langgraph.config import get_config
    config = get_config()
    configurable = config.get("configurable", {})
    user_role = configurable.get("user_role", "")

    # Projects/units/documents are public across all countries. The chatbot
    # does not enforce the user's active country on real-estate data anymore —
    # only leads and tasks are gated, and only by role (handled inside tools).

    # Inject the current user's role so the LLM can gate proposal generation
    role_note = (
        f"\n\n## Current user role\n"
        f"The current user's role is **{user_role or 'unknown'}**. "
        f"Proposal generation is ONLY allowed for roles: company_admin, team_manager, agent. "
        f"If this user's role is not one of those, you MUST refuse any proposal generation "
        f"request politely and do NOT call any proposal-related tool."
    )

    # Give the LLM today's date so it can turn relative phrases such as
    # "tomorrow" / "next Monday" into absolute YYYY-MM-DD values on its own.
    from datetime import date, timedelta
    today = date.today()
    tomorrow = today + timedelta(days=1)
    weekday_name = today.strftime("%A")
    date_note = (
        f"\n\n## Current date\n"
        f"Today is **{weekday_name}, {today.isoformat()}** (YYYY-MM-DD). "
        f"Tomorrow is **{tomorrow.isoformat()}**. "
        f"When the user gives a relative date (e.g. 'tomorrow', 'day after tomorrow', "
        f"'next Monday', 'in 3 days', '10am tomorrow'), you MUST convert it to an "
        f"absolute date in `YYYY-MM-DD` format before calling any tool that takes a "
        f"`due_date`. Never pass relative words to tools \u2014 always pass a real date. "
        f"Ignore the time-of-day part (e.g. '10 am') \u2014 due_date is a date only."
    )

    llm = _get_llm()
    history = _sanitize_messages(list(state["messages"]))
    lc_messages = [SystemMessage(content=SYSTEM_PROMPT + role_note + date_note)] + history
    response = llm.invoke(lc_messages)

    # Log which tools (if any) the LLM decided to call this turn.
    tool_calls = getattr(response, "tool_calls", None) or []
    if tool_calls:
        names = ", ".join(tc.get("name", "?") for tc in tool_calls)
        print(f"\n🤖 [AGENT] LLM requested tool(s): {names}")
    else:
        print("\n🤖 [AGENT] LLM answered directly (no tool used)")

    return {"messages": [response]}


def should_continue(state: State) -> Literal["tools", END]:
    """Route to tools node if LLM made tool calls, otherwise end."""
    last = state["messages"][-1]
    if hasattr(last, "tool_calls") and last.tool_calls:
        return "tools"
    return END


# ── Build graph ───────────────────────────────────────────────────────────────

def _build_graph():
    builder = StateGraph(State)

    builder.add_node("agent", agent_node)
    builder.add_node("tools", ToolNode(ALL_TOOLS))

    builder.add_edge(START, "agent")
    builder.add_conditional_edges("agent", should_continue)
    # After tools execute → back to agent to format the final response
    builder.add_edge("tools", "agent")

    return builder


def _get_db_config():
    """Get database engine type and return ('postgresql'/'sqlite'/None, config)."""
    db = settings.DATABASES["default"]
    engine = db.get("ENGINE", "")
    
    if "postgresql" in engine or "psycopg" in engine:
        return "postgresql", db
    elif "sqlite" in engine:
        return "sqlite", db
    return None, db


# Pre-built graph (no checkpointer — injected per request)
_base_graph = _build_graph()


def _get_graph():
    """
    Return a compiled graph with the appropriate checkpointer.
    - PostgreSQL → PostgresSaver (per call fresh connection)
    - SQLite → SqliteSaver (file-based checkpoint storage)
    - Other → MemorySaver (in-memory, fallback)
    """
    db_type, db = _get_db_config()
    
    # PostgreSQL
    if db_type == "postgresql":
        try:
            import psycopg
            from langgraph.checkpoint.postgres import PostgresSaver
            user = db.get("USER", "")
            password = db.get("PASSWORD", "")
            host = db.get("HOST", "localhost")
            port = db.get("PORT", "5432")
            name = db.get("NAME", "")
            dsn = f"postgresql://{user}:{password}@{host}:{port}/{name}"
            conn = psycopg.connect(dsn, autocommit=True)
            checkpointer = PostgresSaver(conn)
            return _base_graph.compile(checkpointer=checkpointer), conn
        except Exception:
            pass
    
    # SQLite
    if db_type == "sqlite":
        try:
            import sqlite3
            from langgraph.checkpoint.sqlite import SqliteSaver
            db_path = db.get("NAME", "db.sqlite3")
            # Create a connection to the SQLite database
            conn = sqlite3.connect(db_path, check_same_thread=False)
            checkpointer = SqliteSaver(conn)
            return _base_graph.compile(checkpointer=checkpointer), conn
        except Exception:
            pass

    # Fallback to memory
    from langgraph.checkpoint.memory import MemorySaver
    return _base_graph.compile(checkpointer=MemorySaver()), None


# ── Public API ────────────────────────────────────────────────────────────────

def stream_response(
    thread_id: str,
    user_message: str,
    current_country: str = "",
    user_id: int | None = None,
    user_role: str = "",
):
    """
    Generator that yields text chunks for SSE streaming.
    Maintains full conversation history via LangGraph checkpointer.
    thread_id identifies a unique conversation — same thread_id = same memory.
    current_country is injected into config so tools can auto-filter by country.
    user_id / user_role are injected so proposal tools can scope + gate by role.
    """
    compiled_graph, conn = _get_graph()
    config = {
        "configurable": {
            "thread_id": thread_id,
            "current_country": current_country,
            "user_id": user_id,
            "user_role": user_role,
        }
    }
    input_state = {"messages": [HumanMessage(content=user_message)]}

    try:
        for chunk, metadata in compiled_graph.stream(
            input_state, config=config, stream_mode="messages"
        ):
            # Only stream tokens from the final AI text response.
            # Skip: ToolMessage (raw DB results), AIMessage with tool_calls (intermediate),
            # HumanMessage, and any chunk without text content.
            if (
                isinstance(chunk, AIMessage)
                and chunk.content
                and not getattr(chunk, "tool_calls", None)
            ):
                yield chunk.content
    finally:
        if conn:
            conn.close()


def get_history(thread_id: str) -> list[dict]:
    """
    Return conversation history for a thread as a list of
    {'role': 'user'|'assistant', 'content': '...'} dicts.
    """
    compiled_graph, conn = _get_graph()
    config = {"configurable": {"thread_id": thread_id}}
    try:
        state = compiled_graph.get_state(config)
        messages = state.values.get("messages", [])
        result = []
        for msg in messages:
            if isinstance(msg, HumanMessage):
                result.append({"role": "user", "content": msg.content})
            elif isinstance(msg, AIMessage):
                result.append({"role": "assistant", "content": msg.content})
        return result
    except Exception:
        return []
    finally:
        if conn:
            conn.close()
