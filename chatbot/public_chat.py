"""
The website chatbot for visitors who are not logged in.

It answers questions about Nexusproperty from the public website facts only
(chatbot/public_prompt.py). It has no tools, never touches the database and
keeps no history on the server: the browser sends the latest turns with each
message.
"""

from __future__ import annotations

import logging
from asgiref.sync import sync_to_async
from django.conf import settings
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from chatbot.public_prompt import PUBLIC_SYSTEM_PROMPT, get_public_system_prompt

logger = logging.getLogger(__name__)

# Longest message a visitor may send. None = no limit.
PUBLIC_MESSAGE_MAX_CHARS = None
# Earlier turns the browser may send back, and how much of each is kept.
PUBLIC_HISTORY_MESSAGES = 10
PUBLIC_HISTORY_MESSAGE_MAX_CHARS = 2000


def clean_history(raw) -> list:
    """The visitor's earlier turns, as sent by the browser, made safe to use:
    only user/assistant text, keeping turns without artificial limits."""
    if not isinstance(raw, list):
        return []
    messages = []
    items = raw[-PUBLIC_HISTORY_MESSAGES:] if PUBLIC_HISTORY_MESSAGES else raw
    for item in items:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        content = content.strip()
        if PUBLIC_HISTORY_MESSAGE_MAX_CHARS:
            content = content[:PUBLIC_HISTORY_MESSAGE_MAX_CHARS]
        role = item.get("role")
        if role == "user":
            messages.append(HumanMessage(content=content))
        elif role == "assistant":
            messages.append(AIMessage(content=content))
    return messages


def _llm():
    return ChatOpenAI(
        model="gpt-4o-mini",
        temperature=0.3,
        streaming=True,
        max_tokens=500,
        timeout=30,
        max_retries=1,
        openai_api_key=settings.OPENAI_API_KEY,
    )


def get_public_knowledge_context(query: str, top_k: int = 4) -> str:
    """Search Pinecone for public knowledge base chunks matching `query`."""
    try:
        from chatbot.pinecone_service import PineconeService
        results = PineconeService().search_public(query, top_k=top_k)
        if not results:
            return ""
        chunks = []
        for r in results:
            score = r.get("score")
            if score is not None and score < 0.15:
                continue
            text = (r.get("text") or "").strip()
            if text:
                chunks.append(text)
        return "\n\n---\n\n".join(chunks)
    except Exception:
        logger.exception("Failed to query public knowledge base from Pinecone")
        return ""


async def astream_reply(message: str, history: list):
    """Yield the reply to `message` piece by piece."""
    context = await sync_to_async(get_public_knowledge_context, thread_sensitive=False)(message)
    system_prompt = get_public_system_prompt(context)
    messages = [SystemMessage(content=system_prompt), *history, HumanMessage(content=message)]
    async for chunk in _llm().astream(messages):
        if chunk.content:
            yield chunk.content

