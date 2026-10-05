from __future__ import annotations

import hashlib
import json
import logging
import os
from typing import Any

from django.conf import settings

logger = logging.getLogger(__name__)

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_CACHE_DIR = os.path.join(_THIS_DIR, "output")

_MODEL = "gpt-4o"


_FACT_KEYS = (
    "fact_address",
    "location_label",
    "fact_completion",
    "fact_lease_length",
    "fact_building_height",
    "fact_total_units",
    "fact_rental_yield",
    "spec_flooring",
    "spec_kitchen",
    "spec_bathroom",
    "spec_lifts",
    "spec_cycling",
    "gallery_desc_1",
    "gallery_desc_2",
    "gallery_desc_3",
    "gallery_desc_4",
    "market_intro",
    "highlight_title",
    "highlight_body",
    "average_rental_yield",
    "annual_house_price_growth",
    "local_employment_rate",
    "growing_city_population",
    "reservation_fee",
    "mortgage_amount",
    "exchange_percentage",
    "completion_percentage",
    "second_installments_percentage"
)
_LIST_KEYS = ("investment_cases",)
_ALL_KEYS = _FACT_KEYS + _LIST_KEYS


_SYSTEM_PROMPT = (
    "You are an assistant that extracts real-estate development facts from "
    "developer brochures and fact-check PDFs. Return ONLY a JSON object that "
    "matches the requested schema. Use the exact text from the documents "
    "whenever possible. If a value is not stated, return the string \"TBC\"."
)


_USER_INSTRUCTIONS = (
    "Read the project documents below and extract the following fields:\n\n"
    "Development facts (short presentation-ready strings):\n"
    "  fact_address          - Full development address.(e.g. 16 Manor Road, Leeds LS11 9AH) please don't add extra text.\n"
    "  location_label        - A SHORT location label in the exact format "
    "'City, Country' where Country is a short name/abbreviation (UK, USA, UAE, "
    "etc.). Derive it ONLY from the 'PROJECT LOCATION' line given at the very top "
    "of this message (NOT from any address inside the documents). "
    "Example: PROJECT LOCATION = 'Forum House, Everard Close, St. Albans, AL1 2PS' "
    "-> 'St Albans, UK'. No postcode, no street, no extra text.\n"
    "  fact_completion       - Estimated completion date (e.g. 'Q4 2026').\n"
    "  fact_lease_length     - Lease length / tenure (e.g. '250 Years Leasehold').\n"
    "  fact_building_height  - Building height or number of storeys.\n"
    "  fact_total_units      - Total number of units in the development.\n"
    "  fact_rental_yield     - Estimated rental yield, including the % sign.\n\n"
    "Interior specifications (CRITICAL: Every spec_* field MUST be a complete, well-crafted sentence of 12 to 20 words ending with a period. DO NOT return short 2-5 word phrases or fragments! If the document only has a brief mention, expand it into a full, professional 15-word specification sentence):\n"
    "  spec_flooring  - Flooring finish. Must be a complete 12-20 word sentence. (e.g. 'Engineered oak flooring is laid throughout the open-plan living areas with luxury carpets in all bedrooms.').\n"
    "  spec_kitchen   - Kitchen specification. Must be a complete 12-20 word sentence. (e.g. 'Sleek modern kitchens feature fully integrated energy-efficient appliances, quartz worktops, and elegant contemporary cabinetry.').\n"
    "  spec_bathroom  - Bathroom fittings and finish. Must be a complete 12-20 word sentence. (e.g. 'Elegantly appointed bathrooms include premium sanitaryware, chrome fittings, heated towel rails, and porcelain wall tiling.').\n"
    "  spec_lifts     - Lift / elevator provision. Must be a complete 12-20 word sentence. (e.g. 'Secure high-speed passenger lifts provide smooth and convenient access to all residential floors and basement areas.').\n"
    "  spec_cycling   - Cycling / parking provision. Must be a complete 12-20 word sentence. (e.g. 'Dedicated secure cycle storage facilities and allocated parking spaces are provided for residents within the development.').\n\n"
    "Gallery descriptions (each MUST be a single sentence of approximately "
    "20 words, and make sure that sentence is completed and end with full stop (.), describing that part of the development):\n"
    "  gallery_desc_1 - Building exterior / facade and overall architectural design.\n"
    "  gallery_desc_2 - Kitchen and dining area: layout, appliances and finishes.\n"
    "  gallery_desc_3 - Master bedroom: size, finishes and notable features.\n"
    "  gallery_desc_4 - Typical bed sample layout / floor plan highlights.\n\n"
    "Market analysis (city-specific, grounded in document facts):\n"
    "  market_intro       - One paragraph (~70 words , and make sure that paragraph is completed and end with full stop (.)) introducing why the city is a\n"
    "                       compelling buy-to-let location. Embed concrete numbers /\n"
    "                       proper nouns from the documents (e.g. committed investment\n"
    "                       value, university, masterplan name, data source).\n"
    "  investment_cases   - Array of EXACTLY 4 investment-case objects derived from\n"
    "                       the documents. Each object MUST have:\n"
    "                         title - 2-5 word headline (e.g. 'UCLan University Masterplan').\n"
    "                         body  - ~150, and make sure that sentence is completed and end with full stop (.), word paragraph packed with concrete facts and\n"
    "                                 figures lifted from the documents (£ amounts, %\n"
    "                                 figures, student counts, retention rates, etc.).\n"
    "                                 EVERY key fact/figure in the body (money amounts\n"
    "                                 like \u00a37bn / \u00a3200 million, percentages like 40%,\n"
    "                                 counts like 30,000+ students, rankings, named\n"
    "                                 initiatives) MUST be wrapped in an HTML\n"
    "                                 <strong>...</strong> tag so it renders bold in the\n"
    "                                 PDF. Example: 'Leeds is a central figure in the\n"
    "                                 <strong>\u00a37bn Northern Powerhouse initiative</strong>,\n"
    "                                 with an economy valued at <strong>\u00a369bn</strong> that\n"
    "                                 has grown by <strong>40%</strong> over the past decade.'\n"
    "                                 Use ONLY <strong> tags; no markdown or other HTML.\n"
    "                       If the documents do not fully support 4 angles, broaden the\n"
    "                       scope (e.g. location, transport, regeneration, demographics,\n"
    "                       supply-demand, yield ranking) so 4 distinct cases are returned.\n\n"
    "Market highlight (gold-on-navy callout shown at the bottom of the page):\n"
    "  highlight_title - 8-14 word headline citing a specific ranking, statistic or\n"
    "                    award from the documents (e.g. 'Preston: Ranked #1 Small City\n"
    "                    in England for Rental Yields by Savills').\n"
    "  highlight_body  - ~45 word paragraph backing the headline with concrete\n"
    "                    figures and the data source (e.g. 'Savills research identifies\n"
    "                    Preston as a top buy-to-let hotspot with investors achieving\n"
    "                    strong average returns.'). IMPORTANT: do NOT include any\n"
    "                    rental-yield figure or the words 'gross rental yield' in this\n"
    "                    paragraph (e.g. never end with '7.1% gross rental yield') --\n"
    "                    the yield is displayed separately. End the sentence naturally\n"
    "                    WITHOUT the yield number.\n\n"
    "Market stats band -- HONESTY RULES:\n"
    "  * Extract each value ONLY if it is explicitly stated in the provided\n"
    "    documents for THIS city / development. Numbers must be copied verbatim\n"
    "    from the text (same digits, same unit / suffix).\n"
    "  * Do NOT infer, estimate, average, derive, round, or carry numbers across\n"
    "    different metrics. If a number is for a different city, region or year,\n"
    "    do NOT use it.\n"
    "  * Do NOT fabricate plausible-looking figures. If the document does not\n"
    "    state the metric, return exactly \"TBC\".\n"
    "  * Keep the original unit / suffix exactly as written in the source\n"
    "    (e.g. '%', '+', 'K', 'million', '£', 'bn'). Do not convert units.\n"
    "  * Each value must be a short headline string (<= 12 characters where\n"
    "    possible) suitable for a large display tile.\n\n"
    "Fields:\n"
    "  average_rental_yield      - Average rental yield for the city / area,\n"
    "                              as a percentage with the '%' sign\n"
    "                              (e.g. '7.1%', '8.8%'). Only use a figure\n"
    "                              explicitly labelled as a rental yield.\n"
    "  annual_house_price_growth - Annual / yearly house price growth for the\n"
    "                              city or area, with the '%' sign and any\n"
    "                              direction word kept verbatim (e.g. '4.8%',\n"
    "                              'Up 4.8%', '17%'). Do not use long-term\n"
    "                              totals (e.g. '5-year growth') here.\n"
    "  local_employment_rate     - Local employment rate for the city as a\n"
    "                              percentage with the '%' sign (e.g. '80.4%',\n"
    "                              '96%'). Must be labelled as an employment\n"
    "                              rate in the source; do NOT substitute\n"
    "                              unemployment or workforce-size figures.\n"
    "  growing_city_population   - City / area resident population headline\n"
    "                              figure, copied with its original suffix\n"
    "                              (e.g. '162,864+', '1.4 million', '45K+').\n"
    "                              Do NOT use student counts or visitor\n"
    "                              numbers here.\n\n"
    "Payment plan (acquisition structure for the selected unit, taken from the\n"
    "developer's payment schedule. Extract ONLY the headline fee strings and the\n"
    "percentage splits -- amounts will be calculated separately from the unit price):\n"
    "  reservation_fee        - Headline reservation fee to secure the unit\n"
    "                           (e.g. '\u00a35,000'). Include the currency symbol.\n"
    "  mortgage_amount        - Maximum LTV mortgage available at completion as a\n"
    "                           short '<number>% LTV' string only (e.g. 70).\n"
    "                           Do NOT include surrounding marketing phrases like\n"
    "                           'UP TO', 'Mortgages Available', 'Maximum', etc. --\n"
    "                           output ONLY the percentage followed by 'LTV'.\n"
    "  exchange_percentage    - Downpayment percentage paid on exchange of contracts,\n"
    "                           including the '%' sign (e.g. '20%').\n"
    "  completion_percentage  - Balance percentage due on completion, including the\n"
    "                           '%' sign (e.g. '80%' or '70%').\n\n"
    "second_installments_percentage - Percentage of the total purchase price due on the second installment, including the '%' sign (e.g. '20%').\n"
    "If  value isn't stated in the documents, use \"TBC\" for strings or [] for arrays."
)


_INVESTMENT_CASE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["title", "body"],
    "properties": {
        "title": {"type": "string"},
        "body": {"type": "string"},
    },
}

_JSON_SCHEMA = {
    "name": "proposal_facts",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": list(_ALL_KEYS),
        "properties": {
            **{key: {"type": "string"} for key in _FACT_KEYS},
            "investment_cases": {
                "type": "array",
                "items": _INVESTMENT_CASE_SCHEMA,
            },
        },
    },
    "strict": True,
}


def _empty_facts() -> dict[str, Any]:
    facts: dict[str, Any] = {key: "TBC" for key in _FACT_KEYS}
    facts["investment_cases"] = []
    return facts


def documents_fingerprint(project_id: int) -> str | None:
    return _documents_fingerprint(project_id)


def _documents_fingerprint(project_id: int) -> str | None:
    """Return a stable hash of (id, updated_at) for the project's documents."""
    from projects.models import ProjectDocument

    docs = (
        ProjectDocument.objects
        .filter(project_id=project_id)
        .order_by("pk")
        .values("pk", "updated_at")
    )
    if not docs:
        return None

    parts = [f"{d['pk']}:{d['updated_at'].isoformat() if d['updated_at'] else ''}" for d in docs]
    return hashlib.md5("|".join(parts).encode("utf-8")).hexdigest()


def _cache_path(project_id: int, fingerprint: str) -> str:
    project_dir = os.path.join(_CACHE_DIR, str(project_id))
    os.makedirs(project_dir, exist_ok=True)
    return os.path.join(project_dir, f"ai_facts_{fingerprint}.json")


def _load_cached(project_id: int, fingerprint: str) -> dict[str, Any] | None:
    path = _cache_path(project_id, fingerprint)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and all(k in data for k in _ALL_KEYS):
            return _normalise_facts(data)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not read cached AI facts %s: %s", path, exc)
    return None


def _save_cached(project_id: int, fingerprint: str, facts: dict[str, Any]) -> None:
    path = _cache_path(project_id, fingerprint)
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(facts, fh, ensure_ascii=False, indent=2)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not write cached AI facts %s: %s", path, exc)


def _collect_document_text(project_id: int) -> str:
    """Concatenate extracted_text of all PDFs for the project.

    If a document's `extracted_text` is blank (the background Celery task
    hasn't run yet), extract it on-the-fly from the file and persist it
    back to the row so subsequent runs are instant.
    """
    from django.utils import timezone

    from projects.models import ProjectDocument
    from projects.pdf_extractor import extract_document_text

    documents = (
        ProjectDocument.objects
        .filter(project_id=project_id)
        .order_by("label", "pk")
    )

    chunks: list[str] = []
    summary: list[tuple[str, int, int, str]] = []  # (label, pk, chars, source)
    for doc in documents:
        text = (doc.extracted_text or "").strip()
        source = "cache"

        if not text and doc.file and str(doc.file.name).lower().endswith(".pdf"):
            source = "on-the-fly"
            try:
                text = extract_document_text(doc.file).strip()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "On-the-fly text extraction failed for document %s: %s",
                    doc.pk, exc,
                )
                text = ""

            if text:
                try:
                    doc.extracted_text = text
                    doc.extracted_at = timezone.now()
                    doc.save(update_fields=["extracted_text", "extracted_at", "updated_at"])
                    logger.info(
                        "Extracted and cached %d chars for document %s (project %s).",
                        len(text), doc.pk, project_id,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "Could not persist extracted_text for document %s: %s",
                        doc.pk, exc,
                    )

        if not text:
            summary.append((doc.label, doc.pk, 0, "empty"))
            continue

        summary.append((doc.label, doc.pk, len(text), source))
        chunks.append(f"=== DOCUMENT: {doc.label} (id={doc.pk}) ===\n{text}")

    total = sum(s[2] for s in summary)
    print(f"\n[ai_facts] Project {project_id} -- documents sent to LLM:")
    for label, pk, chars, src in summary:
        print(f"  - {label:<12} id={pk:<4} chars={chars:>7,}  ({src})")
    print(f"  TOTAL chars sent to LLM: {total:,}\n")

    logger.info(
        "Project %s: sending %d chars from %d documents to LLM (%s)",
        project_id, total, len([s for s in summary if s[2] > 0]),
        ", ".join(f"{s[0]}:{s[2]}" for s in summary),
    )

    return "\n\n".join(chunks)


def _call_gpt(documents_text: str, project_location: str = "") -> dict[str, str]:
    """Call GPT-4o with the documents and return parsed facts."""
    from openai import OpenAI

    client = OpenAI(api_key=settings.OPENAI_API_KEY)

    location_header = (
        f"PROJECT LOCATION: {project_location.strip()}\n\n"
        if project_location and project_location.strip()
        else ""
    )

    response = client.chat.completions.create(
        model=_MODEL,
        temperature=0,
        response_format={"type": "json_schema", "json_schema": _JSON_SCHEMA},
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"{location_header}{_USER_INSTRUCTIONS}\n\n{documents_text}",
            },
        ],
    )

    content = response.choices[0].message.content or "{}"
    try:
        data: Any = json.loads(content)
    except json.JSONDecodeError as exc:
        logger.warning("GPT returned invalid JSON for proposal facts: %s", exc)
        return _empty_facts()

    if not isinstance(data, dict):
        return _empty_facts()

    return _normalise_facts(data)


def _normalise_facts(data: dict[str, Any]) -> dict[str, Any]:
    """Coerce a raw dict (from GPT or cache) into the expected facts shape."""
    result: dict[str, Any] = {}
    for key in _FACT_KEYS:
        val = str(data.get(key) or "TBC").strip() or "TBC"
        if key.startswith("spec_") and (val == "TBC" or not val):
            val = ""
        result[key] = val

    cases_raw = data.get("investment_cases") or []
    cases: list[dict[str, str]] = []
    if isinstance(cases_raw, list):
        for item in cases_raw[:4]:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            body = str(item.get("body") or "").strip()
            if title and body:
                cases.append({"title": title, "body": body})
    result["investment_cases"] = cases
    return result


def extract_proposal_facts(project_id: int, force_refresh: bool = False) -> dict[str, Any]:
    """Return the six fact_* fields for a project, extracted via GPT-4o."""
    fingerprint = _documents_fingerprint(project_id)
    if fingerprint is None:
        logger.info("No documents found for project %s; returning empty facts.", project_id)
        return _empty_facts()

    if not force_refresh:
        cached = _load_cached(project_id, fingerprint)
        if cached is not None:
            logger.info("Using cached AI facts for project %s.", project_id)
            return cached

    documents_text = _collect_document_text(project_id)
    if not documents_text:
        logger.info("No extracted_text available for project %s documents.", project_id)
        return _empty_facts()

    # Pass the project's own location so `location_label` reflects the actual
    # project address, not any address mentioned inside the documents.
    project_location = ""
    try:
        from projects.models import Project
        project_location = (
            Project.objects.filter(pk=project_id)
            .values_list("location", flat=True)
            .first()
            or ""
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not read location for project %s: %s", project_id, exc)

    try:
        facts = _call_gpt(documents_text, project_location=project_location)
    except Exception as exc:  # noqa: BLE001
        logger.exception("GPT-4o fact extraction failed for project %s: %s", project_id, exc)
        return _empty_facts()

    _save_cached(project_id, fingerprint, facts)
    return facts
