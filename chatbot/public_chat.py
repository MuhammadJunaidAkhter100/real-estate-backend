"""
The website chatbot for visitors who are not logged in.

It answers questions about Nexusproperty from the public website facts only
(chatbot/public_prompt.py). It has no tools, never touches the database and
keeps no history on the server: the browser sends the latest turns with each
message.
"""

from __future__ import annotations

from django.conf import settings
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from chatbot.public_prompt import PUBLIC_SYSTEM_PROMPT

# Longest message a visitor may send.
PUBLIC_MESSAGE_MAX_CHARS = 1000
# Earlier turns the browser may send back, and how much of each is kept.
PUBLIC_HISTORY_MESSAGES = 10
PUBLIC_HISTORY_MESSAGE_MAX_CHARS = 2000


def clean_history(raw) -> list:
    """The visitor's earlier turns, as sent by the browser, made safe to use:
    only user/assistant text, the latest few, each trimmed."""
    if not isinstance(raw, list):
        return []
    messages = []
    for item in raw[-PUBLIC_HISTORY_MESSAGES:]:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        content = content.strip()[:PUBLIC_HISTORY_MESSAGE_MAX_CHARS]
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


async def astream_reply(message: str, history: list):
    """Yield the reply to `message` piece by piece."""
    messages = [SystemMessage(content=PUBLIC_SYSTEM_PROMPT), *history, HumanMessage(content=message)]
    async for chunk in _llm().astream(messages):
        if chunk.content:
            yield chunk.content
