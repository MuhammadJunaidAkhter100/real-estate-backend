from __future__ import annotations

import logging

from django.conf import settings

logger = logging.getLogger(__name__)

_MODEL = "gpt-4o-mini"

_SYSTEM_PROMPT = (
    "You extract a single fact from a real-estate development's fact-check "
    "document: the estimated completion date. Reply with ONLY a short date "
    "string such as 'Q4 2026', 'Q2 2025', 'December 2026' or '2027'. If the "
    "text does not state a completion date, reply with exactly 'TBC'."
)

_USER_INSTRUCTION = (
    "From the document text below, extract the estimated completion date "
    "(e.g. 'Q4 2026'). Return ONLY that short date string, no other text.\n\n"
)




def extract_estimated_completion(text: str) -> str:
    """Return a short completion-date string from fact-checks text, or "".

    Returns an empty string when the text is blank, the model is unavailable,
    or no completion date is stated ("TBC").
    """
    text = (text or "").strip()
    if not text:
        return ""

    try:
        from openai import OpenAI

        client = OpenAI(api_key=settings.OPENAI_API_KEY)
        response = client.chat.completions.create(
            model=_MODEL,
            temperature=0,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": f"{_USER_INSTRUCTION}   {text}"},
            ],
        )
        value = (response.choices[0].message.content or "").strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Estimated-completion extraction failed: %s", exc)
        return ""

    # Normalise: single line, strip quotes/trailing punctuation, drop "TBC".
    value = value.splitlines()[0].strip().strip('"').strip("'").rstrip(".")
    if not value or value.upper() == "TBC":
        return ""
    return value[:100]


def _fact_checks_text(project) -> str:
    """Return the extracted text of the project's fact_checks document, or ""."""
    from projects.models import ProjectDocument

    doc = (
        ProjectDocument.objects
        .filter(project=project, label="fact_checks")
        .exclude(extracted_text="")
        .order_by("-updated_at")
        .first()
    )
    return (doc.extracted_text if doc else "") or ""


def populate_estimated_completion(project, *, force: bool = False) -> str:
    """Populate ``project.estimated_completion`` from its fact_checks text.

    Always re-extracts and overwrites any existing value when a new one is
    found. Returns the value that ends up stored ("" if none could be found).
    """
    existing = (project.estimated_completion or "").strip()

    text = _fact_checks_text(project)
    if not text:
        logger.info(
            "No fact_checks text for project %s; cannot extract completion.",
            project.pk,
        )
        return existing

    value = extract_estimated_completion(text)
    logger.info("new value for project %s estimated_completion: %r", project.pk, value)
    if not value:
        return existing

    project.estimated_completion = value
    project.save(update_fields=["estimated_completion", "updated_at"])
    logger.info(
        "Project %s: estimated_completion set to %r.", project.pk, value,
    )
    return value
