from __future__ import annotations
import json
import logging
from typing import Any, Dict, List, Optional
from django.conf import settings
logger = logging.getLogger(__name__)

AGENT_SPEAKERS = {'agent', 'bot', 'assistant', 'ai'}
CUSTOMER_SPEAKERS = {'user', 'customer', 'caller', 'lead'}


def normalize_speaker(raw_speaker: Any) -> str:
    from calling_agent.models import CallTranscriptTurn

    speaker = str(raw_speaker or '').strip().lower()
    if speaker in AGENT_SPEAKERS:
        return CallTranscriptTurn.Speaker.AGENT
    if speaker in CUSTOMER_SPEAKERS:
        return CallTranscriptTurn.Speaker.CUSTOMER
    if speaker in {'system'}:
        return CallTranscriptTurn.Speaker.SYSTEM
    return CallTranscriptTurn.Speaker.UNKNOWN


def format_transcript_as_text(transcript_input: Any) -> str:
    """Convert transcript data to plain text format.

    Compatible with ElevenLabs Webhooks/Conversational AI payload formats,
    standard speaker/message arrays, and raw string payloads.
    """
    if not transcript_input:
        return ""
    if isinstance(transcript_input, str):
        return transcript_input

    # Support nested ElevenLabs payload objects (e.g. {"transcript": [...]})
    if isinstance(transcript_input, dict):
        if "transcript" in transcript_input and isinstance(transcript_input["transcript"], list):
            transcript_input = transcript_input["transcript"]
        elif "conversation" in transcript_input and isinstance(transcript_input["conversation"], list):
            transcript_input = transcript_input["conversation"]
        elif "messages" in transcript_input and isinstance(transcript_input["messages"], list):
            transcript_input = transcript_input["messages"]

    if isinstance(transcript_input, list):
        lines = []
        for item in transcript_input:
            if isinstance(item, dict):
                # Speaker resolution (ElevenLabs uses 'role', others use 'speaker', 'name', etc.)
                speaker = item.get("speaker") or item.get("role") or item.get("name") or item.get("from") or "Speaker"
                speaker_str = str(speaker).strip()

                if speaker_str.lower() in AGENT_SPEAKERS:
                    speaker_str = "AI Agent"
                elif speaker_str.lower() in CUSTOMER_SPEAKERS:
                    speaker_str = "Customer"

                # Message resolution (ElevenLabs uses 'message' or 'text')
                msg = item.get("message") or item.get("text") or item.get("content") or item.get("transcript") or ""
                if msg:
                    lines.append(f"{speaker_str}: {msg}")
            elif isinstance(item, str):
                lines.append(item)
        return "\n".join(lines)

    return str(transcript_input or "")


def analyze_transcript_with_openai(transcript_text: str) -> Optional[Dict[str, Any]]:
    """Analyze call transcript using OpenAI API to extract summary, sentiments, intent, and tasks."""
    api_key = getattr(settings, "OPENAI_API_KEY", None)
    if not api_key:
        logger.info("OPENAI_API_KEY not configured. Using rule-based fallback analysis.")
        return None

    try:
        from openai import OpenAI

        client = OpenAI(api_key=api_key, max_retries=0)

        system_prompt = (
            "You are an expert real estate AI sales call analyzer.\n"
            "Analyze the provided call transcript and extract structured information strictly grounded in the transcript text.\n\n"
            "STRICT ZERO-HALLUCINATION MANDATE:\n"
            "- You must NEVER invent, assume, or extrapolate facts, tasks, channels (e.g., WhatsApp, Email), appointments (e.g., site visits), or dates not explicitly stated in the transcript.\n"
            "- If a detail, channel, or request is not explicitly mentioned by the speaker in the transcript, DO NOT include it.\n"
            "- If no follow-up task was requested or agreed upon, return an empty task array `\"tasks\": []`.\n\n"
            "JSON OUTPUT FORMAT:\n"
            "{\n"
            '  "summary": "<2-3 sentence factual executive summary of what was actually discussed and agreed upon>",\n'
            '  "lead_status": "<valid lead status code or null>",\n'
            '  "key_sentiments": [\n'
            '    "<short punchy sentiment (3-6 words, e.g. Interested in 2-bed units)>"\n'
            '  ],\n'
            '  "detected_intents": [\n'
            '    "<short punchy intent (3-6 words, e.g. High Buying Intent)>"\n'
            '  ],\n'
            '  "objections": [\n'
            '    "<short objection stated by customer, e.g. Price too high>"\n'
            '  ],\n'
            '  "preferences": [\n'
            '    "<short preference stated by customer, e.g. Prefers 2-bedroom>"\n'
            '  ],\n'
            '  "requirements_changed": false,\n'
            '  "tasks": [\n'
            "    {\n"
            '      "name": "<Short task title of explicit follow-up requested or agreed in transcript>",\n'
            '      "description": "<Detailed context and description of what needs to be done based on the transcript discussion>",\n'
            '      "priority": "high | medium | low",\n'
            '      "status": "pending"\n'
            "    }\n"
            "  ]\n"
            "}\n\n"
            "FIELD RULES:\n"
            "1. 'summary': Strictly factual (2-3 sentences max). Summarize only real discussion points, property mentioned, and explicit outcome.\n"
            "2. 'lead_status': Classify the customer's post-call disposition into EXACTLY ONE of the following codes (or null if none explicitly apply):\n"
            "   [Contact Attempt]\n"
            "   - 'contacted': Answered, conversation happened, no stronger disposition.\n"
            "   - 'no_answer': Did not answer / busy / declined before talking.\n"
            "   - 'call_back_requested': Customer explicitly asked to be contacted or called back later / at another time.\n"
            "   - 'wrong_number': Wrong person or invalid number confirmed in conversation.\n"
            "   - 'unreachable': Repeated failed contact; customer cannot be reached.\n"
            "   [Qualification]\n"
            "   - 'interested': Customer showed general interest in the property/project.\n"
            "   - 'highly_interested': Customer expressed strong/enthusiastic buying intent.\n"
            "   - 'need_more_information': Customer requested brochure, pricing, payment plan, or more detailed property info.\n"
            "   - 'site_visit_requested': Customer requested or agreed to schedule an in-person site visit / viewing.\n"
            "   - 'budget_mismatch': Customer stated property price exceeds budget / too expensive.\n"
            "   - 'location_mismatch': Customer stated they want a different location/city.\n"
            "   - 'not_interested': Customer explicitly stated they have no interest at present.\n"
            "   [Nurturing]\n"
            "   - 'follow_up_required': Customer needs another contact / follow-up call.\n"
            "   - 'brochure_sent': Brochure or project information was shared or requested to be sent.\n"
            "   - 'whatsapp_follow_up': Customer requested to move/continue conversation on WhatsApp.\n"
            "   - 'email_sent': Customer requested details or documentation to be sent via Email.\n"
            "   [Sales Process]\n"
            "   - 'site_visit_scheduled': Customer confirmed a specific date/time for an in-person site viewing.\n"
            "   - 'site_visit_completed': Customer confirmed they already completed / visited the property.\n"
            "   - 'negotiation_ongoing': Customer is actively discussing/negotiating price, payment plan, or discount terms.\n"
            "   - 'documentation_in_progress': Preparing paperwork, KYC, or contracts.\n"
            "   - 'booking_amount_received': Token or reservation deposit amount paid.\n"
            "   - 'unit_reserved': Specific unit locked/reserved for customer.\n"
            "   [Closed]\n"
            "   - 'converted_won': Sale completed / property purchased.\n"
            "   - 'lost_to_competitor': Customer purchased property elsewhere or from a competitor.\n"
            "   - 'lost_no_response': Lead became completely inactive / unreachable.\n"
            "   - 'lost_budget_issue': Lead closed due to inability to afford property.\n"
            "   - 'future_prospect': Customer interested but purchasing in future (e.g. next year).\n"
            "   - 'duplicate_lead': Same lead already exists / duplicate entry.\n"
            "   - null: If none of these specific dispositions were expressed in the transcript.\n"
            "3. 'key_sentiments': 2-4 short, punchy bullets (3-6 words each). Extract ONLY feelings, preferences, or reactions directly expressed by the customer.\n"
            "4. 'detected_intents': 2-4 short, punchy bullets (3-6 words each). Extract ONLY explicit engagement or purchase intent signals from the customer.\n"
            "5. 'objections': 0-4 short bullets for explicit objections (price, location, timing, financing, etc.) stated by the customer.\n"
            "6. 'preferences': 0-4 short bullets for explicit property preferences (bedrooms, location, budget range, investment vs end-use).\n"
            "7. 'requirements_changed': true only if the customer explicitly changed budget, bedrooms, location, or property type during the call.\n"
            "8. 'tasks' (ZERO HALLUCINATION & DEDUPLICATION):\n"
            "   - Extract 'name' (short title) AND 'description' (detailed description of what needs to be done based on the transcript discussion).\n"
            "   - Include ONLY follow-up tasks that the customer EXPLICITLY requested or the agent EXPLICITLY agreed to do post-call.\n"
            "   - Use 'due_days' ONLY for time-bound requests with an explicit deadline (callback tomorrow, viewing on Saturday). Omit 'due_days' for open-ended monitoring tasks such as notify when discount/promotion is available.\n"
            "   - NEVER add site visits, WhatsApp messages, or follow-up calls unless the transcript text specifically contains that request.\n"
            "   - Consolidate rephrased or repeated mentions of the same request into EXACTLY ONE single task.\n"
            "   - DO NOT create tasks for actions that were already answered or completed during the call.\n"
            "Return ONLY valid JSON."
        )

        user_prompt = f"Transcript:\n\n{transcript_text}"

        response = client.chat.completions.create(
            model="gpt-5.4",
            temperature=0.0,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"},
        )

        content = response.choices[0].message.content or ""
        return json.loads(content)
    except Exception as exc:
        logger.warning("OpenAI analysis error or quota limit: %s", exc)
        return None


def analyze_and_process_call(call_instance: Any, user: Optional[Any] = None) -> Any:
    """Analyze a call transcript and persist summary metadata.

    Task creation is handled idempotently by webhook post-call processing.
    """
    from calling_agent.webhook_services import PostCallAnalysisService

    transcript_text = format_transcript_as_text(call_instance.transcript_data)
    if not transcript_text.strip():
        return call_instance

    service = PostCallAnalysisService()
    analysis = service.analyze_call(call_instance)
    if not analysis:
        logger.warning(
            "Transcript analysis returned no result for call %s",
            getattr(call_instance, "public_id", call_instance.pk),
        )
        return call_instance

    service.persist_analysis(call_instance, analysis)
    service.materialize_tasks(call_instance, analysis.get("tasks", []))

    return call_instance


