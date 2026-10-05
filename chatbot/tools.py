"""
LangGraph tools for Axiyon.ai chatbot.

Single generic approach:
  - query_database(model, filters, limit) → handles ALL queries
  - LLM generates filters based on schema context in system prompt
  - No need to write a new tool per use-case
"""

import json
import logging
from decimal import Decimal

from langchain_core.tools import tool
from langchain_core.runnables import RunnableConfig
from django.db.models import Q

logger = logging.getLogger(__name__)


# ── Allowed models + their safe queryable fields ──────────────────────────────

ALLOWED_MODELS = {
    "Project": {
        "import": "projects.models.Project",
        "default_filters": {"project_status": "live"},
        "fields": [
            "id", "title", "associated_country", "location", "developer",
            "estimated_completion",
            "project_type", "status", "project_status", "starting_price",
            "yield_percentage", "currency", "property_category",
            "number_of_units", "bed_1", "bed_2", "bed_3", "studio",
            "description",
        ],
        "related": {
            # ⚠️ Chatbot ALWAYS surfaces AVAILABLE-only counts to the user.
            # `bed_1`, `bed_2`, `bed_3`, `studio`, `number_of_units` on the
            # Project row are TOTALS (all statuses). The `available_*` fields
            # below are the ones the chatbot should quote by default.
            "available_units_count": lambda obj: obj.units.filter(status="available").count(),
            "available_bed_1": lambda obj: obj.units.filter(
                status="available", category__icontains="1 bed"
            ).count(),
            "available_bed_2": lambda obj: obj.units.filter(
                status="available", category__icontains="2 bed"
            ).count(),
            "available_bed_3": lambda obj: obj.units.filter(
                status="available", category__icontains="3 bed"
            ).count(),
            "available_studio": lambda obj: obj.units.filter(
                status="available", category__icontains="studio"
            ).count(),
            "documents": lambda obj: [
                {"label": d.label, "url": d.file.url, "name": d.file.name.split("/")[-1]}
                for d in obj.documents.all() if d.file
            ],
            "images": lambda obj: obj.image if isinstance(obj.image, list) else [],
        },
    },
    "Unit": {
        "import": "projects.models.Unit",
        "default_filters": {"status": "available", "project__project_status": "live"},
        "fields": [
            "id", "label", "category", "floor", "area_m2", "area_ft2",
            "list_price", "discounted_price", "currency", "status", "associated_country",
            "project__id", "project__title", "project__location",
            "project__associated_country", "project__developer",
            "project__starting_price", "project__yield_percentage",
            "project__property_category", "project__number_of_units",
            "project__bed_1", "project__bed_2", "project__bed_3", "project__studio",
        ],
        "related": {
            "floor_plan_url": lambda obj: obj.floor_plan_image.url if obj.floor_plan_image else None,
        },
    },
    "ProjectDocument": {
        "import": "projects.models.ProjectDocument",
        "default_filters": {"project__project_status": "live"},
        "fields": [
            "id", "label", "project__id", "project__title",
            "project__associated_country", "project__location",
        ],
        "related": {
            "url": lambda obj: obj.file.url if obj.file else None,
            "filename": lambda obj: obj.file.name.split("/")[-1] if obj.file else None,
        },
    },
    "Lead": {
        "import": "users.models.Lead",
        "default_filters": {},
        "fields": [
            "id", "name", "email", "phone_no", "source", "country",
            "desired_country", "desired_location", "estimated_budget",
            "category", "type", "status", "scheduled_at",
            "project__id", "project__title", "project__associated_country",
        ],
        "related": {},
    },
    "Call": {
        "import": "calling_agent.models.Call",
        "default_filters": {},
        "fields": [
            "id", "public_id", "lead_name", "phone_number", "outbound_number",
            "direction", "trigger", "status", "duration_seconds",
            "failure_code", "failure_detail", "summary", "key_sentiments",
            "detected_intents", "scheduled_for", "initiated_at", "answered_at",
            "ended_at", "created_at", "updated_at",
            "lead__id", "lead__name", "lead__phone_no", "lead__status",
            "company__id", "company__name",
            "context_user__id", "context_user__first_name", "context_user__last_name",
        ],
        "related": {
            "display_lead_name": lambda obj: (obj.lead.name if obj.lead and obj.lead.name else obj.lead_name) or "Unknown",
            "duration_formatted": lambda obj: obj.duration,
            "recording_available": lambda obj: obj.recording_available,
            "transcript_summary": lambda obj: obj.summary,
            "key_sentiments": lambda obj: obj.key_sentiments,
            "detected_intents": lambda obj: obj.detected_intents,
        },
    },
}

# Allowed Django ORM filter suffixes (no write operations)
ALLOWED_SUFFIXES = {
    "exact", "iexact", "icontains", "contains",
    "gte", "lte", "gt", "lt", "in",
    "isnull", "range",
}


def _get_model_class(model_name: str):
    from importlib import import_module
    config = ALLOWED_MODELS[model_name]
    module_path, class_name = config["import"].rsplit(".", 1)
    module = import_module(module_path)
    return getattr(module, class_name)


def _validate_filters(model_name: str, filters: dict) -> dict:
    """Strip any filter keys that are not in the allowed field list to prevent abuse."""
    allowed = set(ALLOWED_MODELS[model_name]["fields"])
    safe = {}
    for key, value in filters.items():
        parts = key.split("__")
        # Find the longest prefix that matches an allowed field.
        matched_len = 0
        for i in range(1, len(parts) + 1):
            candidate = "__".join(parts[:i])
            if candidate in allowed:
                matched_len = i
        if matched_len == 0:
            continue
        # If the entire key equals an allowed field (no trailing lookup),
        # accept it as an exact-match filter (e.g. "project__title": "London House").
        if matched_len == len(parts):
            safe[key] = value
            continue
        # Otherwise, the tail must be a valid Django ORM lookup suffix.
        suffix = parts[-1]
        if suffix in ALLOWED_SUFFIXES:
            safe[key] = value
    return safe


def _to_float(value):
    """Best-effort float parse; returns None if not numeric."""
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _normalise_area_filters(filters: dict, field: str) -> None:
    """Make area filters inclusive of the stored decimal part, in place.

    Areas are stored with two decimals (e.g. 452.00). This rewrites a user's
    whole-number area query so it matches the decimals too:

      * exact ``area_ft2 = 452``           -> 452 <= area < 453
      * ``area_ft2__exact = 452``          -> 452 <= area < 453
      * ``area_ft2__gte = 452``            -> unchanged (already inclusive)
      * ``area_ft2__lte = 679``            -> area < 680  (keeps 679.99)
      * ``area_ft2__gt  = 452``            -> area >= 453
      * ``area_ft2__lt  = 679``            -> unchanged

    A combined range (``__gte`` + ``__lte``) therefore covers min.00 through
    max.99 inclusive.
    """
    exact_key = field
    exact_suffix_key = f"{field}__exact"

    # 1) Exact match -> half-open [n, n+1) so all decimals of n are included.
    for key in (exact_key, exact_suffix_key):
        if key in filters:
            n = _to_float(filters.pop(key))
            if n is None:
                continue
            lo = float(int(n))  # floor to whole number the user meant
            filters[f"{field}__gte"] = lo
            filters[f"{field}__lt"] = lo + 1.0

    # 2) Upper bound: __lte n -> __lt n+1 so n.01..n.99 are still included.
    lte_key = f"{field}__lte"
    if lte_key in filters:
        n = _to_float(filters.pop(lte_key))
        if n is not None:
            filters[f"{field}__lt"] = float(int(n)) + 1.0

    # 3) Strict upper (__gt n) -> __gte n+1 so we skip the whole n.xx band.
    gt_key = f"{field}__gt"
    if gt_key in filters:
        n = _to_float(filters.pop(gt_key))
        if n is not None:
            filters[f"{field}__gte"] = float(int(n)) + 1.0

    # (__gte and __lt are already inclusive/half-open as desired.)


@tool
def query_database(
    model: str,
    filters: dict,
    limit: int = 10,
    fields: list = None,
    config: RunnableConfig = None,
) -> str:
    """
    Query the Axiyon.ai real estate database. Use this for ALL data questions.

    Available models:
      - "Project": live real estate projects
        Fields: id, title, associated_country, location, developer,
                estimated_completion, project_type, status, starting_price,
                yield_percentage, currency, property_category,
                number_of_units, bed_1, bed_2, description
        Auto-filter: project_status=live

      - "Unit": available units inside projects
        Fields: id, label, category, floor, area_m2, area_ft2, list_price,
                discounted_price, currency, status, associated_country,
                project__title, project__location, project__associated_country,
                project__developer
        Auto-filter: status=available, project__project_status=live

    Filter syntax — standard Django ORM lookups:
      {"associated_country__icontains": "UAE"}
      {"location__icontains": "Dubai"}
      {"starting_price__lte": 500000}
      {"yield_percentage__gte": 7}
      {"project_type__iexact": "residential"}
      {"category__icontains": "2 Bed"}

    Range / between filters (use gte + lte together, or range):
      area between 500 and 800 sq ft:
        {"area_ft2__gte": 500, "area_ft2__lte": 800}
      list price between 200k and 400k:
        {"list_price__gte": 200000, "list_price__lte": 400000}
      discounted price up to 350k:
        {"discounted_price__lte": 350000}
      discounted price between 200k and 300k:
        {"discounted_price__range": [200000, 300000]}

    Area queries (area_ft2 / area_m2): areas are stored with decimals (e.g.
    452.00). Just pass the whole number the user said — the server automatically
    makes it inclusive of the decimals:
      "units of 452 sq ft":            {"area_ft2": 452}       (matches 452.00–452.99)
      "units from 452 to 679 sq ft":   {"area_ft2__gte": 452, "area_ft2__lte": 679}
                                        (matches 452.00 through 679.99)
    Do NOT try to add decimals yourself; send the integer value.

    Args:
        model: "Project" or "Unit"
        filters: dict of Django ORM filter kwargs
        limit: max rows to return (default 10, max 20)
        fields: list of field names to include in output (None = all allowed fields)
    """
    print(f"\n[TOOL] DATABASE QUERY -> model={model} | filters={filters} | limit={limit}")
    logger.info("Tool used: query_database | model=%s | filters=%s", model, filters)

    if model not in ALLOWED_MODELS:
        return f"Unknown model '{model}'. Allowed: {list(ALLOWED_MODELS.keys())}"

    # limit <= 0 (or invalid) → return ALL matching records (no cap).
    if not isinstance(limit, int) or limit <= 0:
        limit = None
    model_config = ALLOWED_MODELS[model]
    ModelClass = _get_model_class(model)

    # Apply default safety filters + user filters. Projects, Units and
    # ProjectDocuments are public across countries — any user can query them
    # regardless of their active country.
    safe_filters = _validate_filters(model, filters)

    # Hard rule: chatbot must only ever surface AVAILABLE units. Strip any
    # attempt (by the LLM or a client) to override the unit status filter so
    # reserved / sold units are never returned.
    if model == "Unit":
        for key in list(safe_filters.keys()):
            if key == "status" or key.startswith("status__"):
                safe_filters.pop(key, None)
    if model in ("Project", "ProjectDocument", "Lead"):
        # Similarly, keep the model-level default filters authoritative — the
        # LLM cannot change `project_status` or `project__project_status` to
        # sneak in draft data.
        for key in list(safe_filters.keys()):
            if key in ("project_status", "project__project_status") or key.startswith(
                ("project_status__", "project__project_status__")
            ):
                safe_filters.pop(key, None)

    # Area queries are inclusive of the decimal part. Areas are stored with two
    # decimals (e.g. 452.00). A user asking for "452 sq ft" should match 452.00
    # through 452.99, and a range "452 to 679" should include everything up to
    # 679.99. Normalise any exact / gte / lte / gt / lt area filter accordingly.
    if model == "Unit":
        _normalise_area_filters(safe_filters, "area_ft2")
        _normalise_area_filters(safe_filters, "area_m2")

    combined_filters = {**model_config["default_filters"], **safe_filters}

    # Leads are role-scoped (superadmin → all, company_admin → whole company,
    # team_manager → managed team, agent → own). Country is NOT enforced.
    def _build_base_queryset():
        if model == "Lead":
            user = _get_user(config)
            if user is None:
                return None
            return _leads_for_user(user)
        if model == "Call":
            user = _get_user(config)
            if user is None:
                return None
            from calling_agent.services import calls_visible_to_user
            return calls_visible_to_user(user).select_related("lead", "company", "context_user")
        return ModelClass.objects.all()

    try:
        base_qs = _build_base_queryset()
        if base_qs is None:
            return json.dumps({"error": "not_authenticated"})
        base_qs = base_qs.filter(**combined_filters).distinct()
        total_count = base_qs.count()
        qs = base_qs if limit is None else base_qs[:limit]
        rows = list(qs.values(*model_config["fields"]))
    except Exception as e:
        return f"Query error: {e}"

    if not rows:
        return f"No {model} records found for the given filters."

    # Add computed related fields
    if model_config["related"]:
        rel_qs = _build_base_queryset().filter(**combined_filters).distinct()
        rel_qs = rel_qs if limit is None else rel_qs[:limit]
        id_to_obj = {obj.pk: obj for obj in rel_qs}
        for row in rows:
            obj = id_to_obj.get(row.get("id"))
            if obj:
                for key, fn in model_config["related"].items():
                    try:
                        row[key] = fn(obj)
                    except Exception:
                        pass

    # Filter output fields if requested
    if fields:
        allowed_set = set(model_config["fields"])
        fields = [f for f in fields if f in allowed_set]
        rows = [{k: v for k, v in row.items() if k in fields} for row in rows]

    # Serialize Decimal values
    for row in rows:
        for k, v in row.items():
            if isinstance(v, Decimal):
                row[k] = float(v)

    return json.dumps(
        {"total_count": total_count, "count": len(rows), "results": rows},
        indent=2, default=str,
    )


# ── Proposal automation tools ─────────────────────────────────────────────────

_PROPOSAL_ROLES = {"axiyon_admin", "company_admin", "team_manager", "agent"}


def _get_user(config: RunnableConfig):
    """Resolve the current user from the LangGraph config, or None."""
    if not config:
        return None
    user_id = config.get("configurable", {}).get("user_id")
    if not user_id:
        return None
    from django.contrib.auth import get_user_model
    User = get_user_model()
    return User.objects.filter(pk=user_id).first()


def _role_allowed(config: RunnableConfig) -> bool:
    role = (config or {}).get("configurable", {}).get("user_role", "") or ""
    return role in _PROPOSAL_ROLES


def _leads_for_user(user):
    """
    Role-aware base Lead queryset (mirrors LeadViewSet.get_queryset):
      - superadmin   → all leads
      - company_admin→ leads of their company
      - team_manager → leads of their managed team (or own)
      - agent/other  → leads they created or are assigned to

    Country is intentionally NOT applied — leads are visible across all
    countries, gated purely by role.
    """
    from django.db.models import Q
    from users.models import Lead

    qs = Lead.objects.select_related("project", "created_by", "assigned_to")
    role = getattr(user, "role", None)

    if role == "superadmin":
        return qs
    if role in ("axiyon_admin", "company_admin"):
        return qs.filter(created_by__company=user.company)
    if role == "team_manager":
        managed_team = getattr(user, "managed_team", None)
        if managed_team:
            return qs.filter(
                Q(assigned_to__team=managed_team)
                | Q(created_by__team=managed_team)
                | Q(assigned_to=user)
            ).distinct()
        return qs.filter(Q(assigned_to=user) | Q(created_by=user)).distinct()
    return qs.filter(Q(assigned_to=user) | Q(created_by=user)).distinct()


@tool
def list_my_leads(config: RunnableConfig = None) -> str:
    """
    PROPOSAL WORKFLOW ONLY. Use this ONLY when the user has explicitly asked to
    GENERATE or CREATE a proposal and you need to pick which lead it is for.
    Do NOT use this to answer general questions such as "how many leads do I have"
    or "list my leads" — for those use query_database("Lead", {...}) instead.

    Returns each lead's id, name, country and the project currently assigned to it.
    Only company_admin, team_manager and agent roles may use this.
    """
    if not _role_allowed(config):
        return json.dumps({
            "error": "permission_denied",
            "detail": "Only company_admin, team_manager and agent roles can generate proposals.",
        })

    user = _get_user(config)
    if user is None:
        return json.dumps({"error": "not_authenticated"})

    qs = _leads_for_user(user)

    leads = []
    for lead in qs[:50]:
        leads.append({
            "lead_id": lead.id,
            "name": lead.name,
            "country": lead.country,
            "desired_country": lead.desired_country,
            "assigned_project_id": lead.project_id,
            "assigned_project_title": lead.project.title if lead.project else None,
        })

    if not leads:
        return json.dumps({"count": 0, "results": [], "detail": "No leads found for your role."})

    return json.dumps({"count": len(leads), "results": leads}, indent=2)


@tool
def list_lead_projects(lead_id: int, config: RunnableConfig = None) -> str:
    """
    Show the project assigned to a given lead — but ONLY if that project is live and
    has at least one available unit (otherwise no proposal can be generated for it).
    Call this after the user picks a lead.

    Args:
        lead_id: the id of the lead chosen by the user (from list_my_leads).
    """
    if not _role_allowed(config):
        return json.dumps({"error": "permission_denied"})

    user = _get_user(config)
    if user is None:
        return json.dumps({"error": "not_authenticated"})

    from users.models import Lead

    lead = (
        Lead.objects
        .select_related("project")
        .filter(pk=lead_id, created_by__company_id=user.company_id)
        .first()
    )
    if lead is None:
        return json.dumps({"error": "lead_not_found", "detail": "Lead not found for your company."})

    project = lead.project
    if project is None:
        return json.dumps({
            "lead_id": lead.id,
            "count": 0,
            "results": [],
            "detail": "This lead has no project assigned yet.",
        })

    available_units = project.units.filter(status="available").count()
    if available_units == 0:
        return json.dumps({
            "lead_id": lead.id,
            "count": 0,
            "results": [],
            "detail": f"Project '{project.title}' has no available units, so no proposal can be generated.",
        })

    return json.dumps({
        "lead_id": lead.id,
        "count": 1,
        "results": [{
            "project_id": project.id,
            "title": project.title,
            "location": project.location,
            "associated_country": project.associated_country,
            "developer": project.developer,
            "available_units_count": available_units,
        }],
    }, indent=2)


@tool
def list_project_units(project_id: int, config: RunnableConfig = None) -> str:
    """
    List the available units of a project so the user can choose one for the proposal.
    Call this after the user confirms the project.

    Args:
        project_id: the id of the project (from list_lead_projects).
    """
    if not _role_allowed(config):
        return json.dumps({"error": "permission_denied"})

    from projects.models import Unit

    units = Unit.objects.select_related("project").filter(
        project_id=project_id, status="available", project__project_status="live",
    )[:50]

    results = []
    for unit in units:
        results.append({
            "unit_id": unit.id,
            "label": unit.label,
            "category": unit.category,
            "floor": unit.floor,
            "area_ft2": float(unit.area_ft2) if unit.area_ft2 is not None else None,
            "list_price": float(unit.list_price) if unit.list_price is not None else None,
            "discounted_price": float(unit.discounted_price) if unit.discounted_price is not None else None,
            "currency": unit.currency,
        })

    if not results:
        return json.dumps({"count": 0, "results": [], "detail": "No available units for this project."})

    return json.dumps({"count": len(results), "results": results}, indent=2)


@tool
def generate_proposal(
    lead_id: int,
    project_id: int,
    unit_id: int,
    config: RunnableConfig = None,
) -> str:
    """
    Generate a proposal PDF for a specific lead + project + unit and return its URL.
    Only call this once the user has confirmed all three: lead, project and unit.
    The unit must belong to the project and be available; the project must be the one
    assigned to the lead.

    Args:
        lead_id: chosen lead id.
        project_id: chosen project id.
        unit_id: chosen available unit id.
    """
    if not _role_allowed(config):
        return json.dumps({
            "error": "permission_denied",
            "detail": "Only company_admin, team_manager and agent roles can generate proposals.",
        })

    user = _get_user(config)
    if user is None:
        return json.dumps({"error": "not_authenticated"})

    from users.models import Lead
    from projects.models import Project, Unit
    from new_proposal.models import GeneratedProposal
    from new_proposal.tasks import check_proposal_quota, generate_proposal_pdf_task

    # Tell the assistant about the plan cap instead of failing mid-generation.
    try:
        check_proposal_quota(user)
    except Exception as exc:  # noqa: BLE001 - surfaced to the assistant as JSON
        limit_error = getattr(exc, "default_code", None)
        if limit_error:
            return json.dumps({
                "error": "plan_limit_reached",
                "code": limit_error,
                "kind": getattr(exc, "kind", None),
                "limit": getattr(exc, "limit", None),
                "current": getattr(exc, "current", None),
                "detail": str(exc.detail),
            })
        raise

    # Validate lead belongs to user's company and is linked to the project
    lead = Lead.objects.filter(pk=lead_id, created_by__company_id=user.company_id).first()
    if lead is None:
        return json.dumps({"error": "lead_not_found", "detail": "Lead not found for your company."})
    if lead.project_id != project_id:
        return json.dumps({
            "error": "project_mismatch",
            "detail": "The selected project is not the one assigned to this lead.",
        })

    project = Project.objects.filter(pk=project_id).first()
    if project is None:
        return json.dumps({"error": "project_not_found"})

    unit = Unit.objects.filter(pk=unit_id, project_id=project_id).first()
    if unit is None:
        return json.dumps({
            "error": "unit_mismatch",
            "detail": "The selected unit does not belong to the given project.",
        })
    if unit.status != "available":
        return json.dumps({
            "error": "unit_unavailable",
            "detail": f"Unit '{unit.label}' is not available (status: {unit.status}).",
        })

    proposal = GeneratedProposal.objects.create(
        project=project,
        lead=lead,
        unit=unit,
        generated_by=user,
        status=GeneratedProposal.Status.PENDING,
    )

    # Run synchronously so we can return the PDF URL directly in the chat.
    try:
        result = generate_proposal_pdf_task.apply(args=[proposal.pk]).get()
    except Exception as exc:  # noqa: BLE001
        return json.dumps({
            "error": "generation_failed",
            "proposal_id": proposal.pk,
            "detail": str(exc),
        })

    proposal.refresh_from_db()
    if proposal.status != GeneratedProposal.Status.COMPLETED:
        return json.dumps({
            "error": "generation_failed",
            "proposal_id": proposal.pk,
            "detail": proposal.error or (result or {}).get("error", "Unknown error."),
        })

    pdf_url = proposal.hosted_url
    if not pdf_url and proposal.file:
        try:
            pdf_url = proposal.file.url
        except Exception:  # noqa: BLE001
            pdf_url = ""

    return json.dumps({
        "success": True,
        "proposal_id": proposal.pk,
        "lead": lead.name,
        "project": project.title,
        "unit": unit.label,
        "pdf_url": pdf_url,
    }, indent=2)


@tool
def send_proposal_to_lead(
    proposal_id: int,
    lead_id: int = None,
    phone_no: str = None,
    config: RunnableConfig = None,
) -> str:
    """
    Send a generated proposal PDF URL to a lead via WhatsApp.

    Before sending, this tool automatically verifies:
      1. The proposal exists and (if lead_id is provided) belongs to the specified lead.
      2. The phone number (either from the lead record or typed manually by the user) is checked
         on WhatsApp via check_number_exists. If the number is not registered on WhatsApp,
         the message is NOT sent and an error is returned.

    Args:
        proposal_id: ID of the generated proposal (from generate_proposal).
        lead_id: ID of the lead (optional if proposal_id is given).
        phone_no: optional phone number typed directly by the user in chat (if empty, uses the lead's phone_no).
    """
    print(f"\n📲 [TOOL] SEND PROPOSAL TO LEAD → proposal_id={proposal_id} | lead_id={lead_id} | phone={phone_no!r}")
    logger.info("Tool used: send_proposal_to_lead | proposal_id=%s | lead_id=%s", proposal_id, lead_id)

    if not _role_allowed(config):
        return json.dumps({
            "error": "permission_denied",
            "detail": "Only company_admin, team_manager and agent roles can send proposals.",
        })

    user = _get_user(config)
    if user is None:
        return json.dumps({"error": "not_authenticated"})

    from new_proposal.models import GeneratedProposal
    from whatsapp.services import WhatsAppService

    proposal = (
        GeneratedProposal.objects
        .select_related("lead", "project", "unit")
        .filter(pk=proposal_id)
        .first()
    )
    if proposal is None:
        return json.dumps({
            "error": "proposal_not_found",
            "detail": f"Proposal #{proposal_id} was not found.",
        })

    # Verify proposal is related to the lead (if lead_id provided)
    if lead_id and proposal.lead_id != lead_id:
        return json.dumps({
            "error": "proposal_lead_mismatch",
            "detail": f"Proposal #{proposal_id} is for lead #{proposal.lead_id} ({proposal.lead.name}), not lead #{lead_id}.",
        })

    lead = proposal.lead

    # Resolve phone number (use user-typed phone_no if given, else lead's phone_no)
    target_phone = (phone_no or "").strip() or (lead.phone_no or "").strip()
    if not target_phone:
        return json.dumps({
            "error": "missing_phone_number",
            "detail": f"No phone number found for lead '{lead.name}' and none was provided.",
        })

    # Get proposal PDF link or file
    pdf_url = proposal.hosted_url
    if not pdf_url and proposal.file:
        try:
            pdf_url = proposal.file.url
        except Exception:  # noqa: BLE001
            pdf_url = ""

    # Prepare PDF file for WhatsApp document attachment
    from django.core.files.base import ContentFile
    import httpx

    pdf_bytes = None
    clean_title = "".join(c for c in (proposal.project.title if proposal.project else "Project") if c.isalnum() or c in (" ", "_", "-")).strip().replace(" ", "_")
    filename = f"Proposal_{clean_title}_{proposal_id}.pdf"

    if proposal.file:
        try:
            proposal.file.open("rb")
            pdf_bytes = proposal.file.read()
            proposal.file.close()
        except Exception as e:
            logger.warning("Could not read proposal.file directly: %s", e)
            pdf_bytes = None

    if not pdf_bytes and pdf_url:
        try:
            resp = httpx.get(pdf_url, timeout=30, follow_redirects=True)
            if resp.status_code == 200:
                pdf_bytes = resp.content
        except Exception as e:
            logger.warning("Could not download proposal from pdf_url (%s): %s", pdf_url, e)

    if not pdf_bytes:
        return json.dumps({
            "error": "proposal_file_missing",
            "detail": f"Proposal #{proposal_id} PDF file could not be read or downloaded.",
        })

    file_obj = ContentFile(pdf_bytes, name=filename)
    file_obj.content_type = "application/pdf"

    service = WhatsAppService()

    # Step 1: Check if phone number is registered on WhatsApp
    check_res = service.check_number_exists(user=user, phone=target_phone)
    if check_res.get("status") != "success":
        return json.dumps({
            "error": "whatsapp_check_failed",
            "detail": check_res.get("message", "Failed to verify phone number on WhatsApp."),
        })

    if not check_res.get("numberExists"):
        return json.dumps({
            "error": "number_not_on_whatsapp",
            "phone": target_phone,
            "detail": f"The phone number '{target_phone}' is NOT registered on WhatsApp. Proposal message was not sent.",
        })

    # Step 2: Use normalized / target chat ID returned by WhatsApp check
    target_chat_id = check_res.get("normalized_chat_id") or check_res.get("chatId") or f"{target_phone.lstrip('+')}@s.whatsapp.net"

    # Step 3: Construct caption message and send PDF Document via WhatsApp
    project_title = proposal.project.title if proposal.project else "Project"
    unit_label = proposal.unit.label if proposal.unit else "Unit"
    lead_name = lead.name if lead else "Valued Client"

    caption_text = (
        f"Hello {lead_name},\n\n"
        f"Here is your proposal document for {project_title} (Unit {unit_label}).\n\n"
        f"Thank you!"
    )

    send_res = service.send_document_message(
        user=user,
        payload={
            "chat_id": target_chat_id,
            "caption": caption_text,
            "file": file_obj,
        },
    )

    if send_res.get("status") in ("error", "invalid_payload", "not_found"):
        return json.dumps({
            "error": "send_message_failed",
            "detail": send_res.get("message", "Failed to send WhatsApp document."),
            "waha_response": send_res,
        })

    return json.dumps({
        "success": True,
        "proposal_id": proposal.pk,
        "lead_id": lead.id if lead else None,
        "lead_name": lead.name if lead else None,
        "phone_no": target_phone,
        "whatsapp_chat_id": target_chat_id,
        "pdf_url": pdf_url,
        "detail": f"Proposal PDF document successfully sent to {lead.name} ({target_phone}) via WhatsApp and recorded in database.",
    }, indent=2)


@tool
def search_knowledge_base(query: str, config: RunnableConfig = None) -> str:
    """
    Search the user's uploaded knowledge-base documents (PDF, TXT, CSV, XLSX)
    stored in the vector database, and return the most relevant passages.

    Use this whenever the user asks a question whose answer may be found in
    THEIR uploaded documents / knowledge base (e.g. "what does my document say
    about ...", "according to the uploaded file ...", or any company-specific
    info not in the projects/units database).

    Args:
        query: the user's natural-language question to search for.
    """
    print(f"\n📚 [TOOL] KNOWLEDGE BASE SEARCH → query={query!r}")
    logger.info("Tool used: search_knowledge_base | query=%s", query)

    query = (query or "").strip()
    if not query:
        return json.dumps({"results": [], "detail": "Empty query."})

    user_id = (config or {}).get("configurable", {}).get("user_id")

    try:
        from chatbot.pinecone_service import PineconeService
        service = PineconeService()
        # Search all knowledge-base documents and project documents for every user.
        metadata_filter = None
        matches = service.search(query, top_k=5, metadata_filter=metadata_filter)
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"results": [], "error": str(exc)})

    # Pinecone always returns top_k results even when they are only loosely
    # related. Use a low threshold so genuinely relevant content passes, but
    # still hard-block truly unrelated results.
    SCORE_THRESHOLD = 0.15
    strong = [m for m in matches if (m.get("score") or 0) >= SCORE_THRESHOLD]

    top_score = max((m.get("score") or 0) for m in matches) if matches else 0
    print(
        f"📚 [TOOL] KNOWLEDGE BASE → {len(matches)} raw matches, "
        f"{len(strong)} above threshold {SCORE_THRESHOLD} "
        f"(top score={top_score:.3f})"
    )

    if not strong:
        # STRICT refusal payload. The prompt tells the LLM to reply with this
        # exact wording; we also mark answerable=False so the agent cannot use
        # the passages to fabricate an answer.
        return json.dumps({
            "answerable": False,
            "results": [],
            "top_score": round(top_score, 3),
            "must_reply_with": (
                "Sorry, the answer to that is not available in our knowledge base."
            ),
            "detail": (
                "No sufficiently relevant passages were found. You MUST reply "
                "verbatim (or a very close paraphrase) with the 'must_reply_with' "
                "text and nothing else. Do NOT answer from general knowledge, do "
                "NOT explain, do NOT add caveats or definitions."
            ),
        })

    results = [
        {
            "text": m["text"],
            "source": m["metadata"].get("original_filename", ""),
            "score": m["score"],
        }
        for m in strong
    ]
    return json.dumps({
        "answerable": True,
        "count": len(results),
        "results": results,
        "instruction": (
            "Answer ONLY using the 'text' fields above. If those passages do not "
            "actually address the user's question, set your answer to: "
            "'Sorry, the answer to that is not available in our knowledge base.' "
            "Never blend in general knowledge or training data."
        ),
    }, indent=2)


# ── Task management tool (create + retrieve in one) ───────────────────────────

# Friendly input → model value normalisation maps.
_TASK_PRIORITY_MAP = {
    "high": "high", "medium": "medium", "med": "medium", "low": "low",
}
_TASK_STATUS_MAP = {
    "todo": "pending", "to do": "pending", "pending": "pending", "new": "pending",
    "inprogress": "in_progress", "in progress": "in_progress",
    "in_progress": "in_progress", "doing": "in_progress",
    "completed": "completed", "complete": "completed", "done": "completed",
}


_WEEKDAYS = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}


def _parse_due_date(value):
    """
    Parse a due-date value into a `date`. Accepts:
      * ISO / common numeric formats (YYYY-MM-DD, DD-MM-YYYY, DD/MM/YYYY, MM/DD/YYYY)
      * relative words: today, tomorrow, day after tomorrow, tmrw, tmr
      * "in N days" / "N days later"
      * "next monday", "this friday", or a bare weekday name
      * a plain `date` / `datetime` (returned/normalised as-is)
    Returns `None` if the input can't be interpreted.
    """
    if not value:
        return None

    from datetime import date, datetime, timedelta

    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value

    text = str(value).strip().lower()
    if not text:
        return None

    today = date.today()

    if text in ("today", "tdy"):
        return today
    if text in ("tomorrow", "tmrw", "tmr", "tom"):
        return today + timedelta(days=1)
    if text in ("day after tomorrow", "day-after-tomorrow"):
        return today + timedelta(days=2)
    if text in ("yesterday",):
        return today - timedelta(days=1)

    # "in 3 days" / "3 days later" / "after 5 days"
    import re
    m = re.match(r"^(?:in|after)\s+(\d+)\s+day(?:s)?$", text)
    if m:
        return today + timedelta(days=int(m.group(1)))
    m = re.match(r"^(\d+)\s+day(?:s)?\s+later$", text)
    if m:
        return today + timedelta(days=int(m.group(1)))

    # "next week" / "next month"
    if text == "next week":
        return today + timedelta(days=7)
    if text == "next month":
        # naive: add 30 days
        return today + timedelta(days=30)

    # weekday phrases: "next monday", "this friday", or bare "friday"
    m = re.match(r"^(?:next|this|coming|on)\s+([a-z]+)$", text) or re.match(r"^([a-z]+)$", text)
    if m:
        wd = _WEEKDAYS.get(m.group(1))
        if wd is not None:
            delta = (wd - today.weekday()) % 7
            if delta == 0:
                delta = 7  # "next monday" on a Monday → the following Monday
            return today + timedelta(days=delta)

    # Numeric formats.
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%m/%d/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


@tool
def manage_tasks(
    action: str,
    name: str = None,
    lead_id: int = None,
    priority: str = "medium",
    due_date: str = None,
    status: str = "todo",
    config: RunnableConfig = None,
) -> str:
    """
    Create a task or list the current user's tasks. Any logged-in role may use this.

    action:
      - "create": create a new task. Required: name. Optional: lead_id, priority,
                  due_date, status.
      - "list":   return tasks visible to the current user by role
                  (superadmin: all; company_admin: whole company;
                   team_manager: own + team; agent: own only).

    Field rules:
      - name:      short title of the task (required for create).
      - lead_id:   the id of a lead this task relates to (optional). To pick one,
                   FIRST call query_database("Lead", {}) to show the user the lead
                   NAMES, let them choose, then pass that lead's id here. Only the
                   lead_id is stored; show names to the user.
      - priority:  "high" | "medium" | "low" (default "medium").
      - due_date:  date string "YYYY-MM-DD" (optional).
      - status:    "todo" | "inprogress" | "completed" (default "todo").
      - associated_country: NEVER ask the user — it is automatically set to the
                   current user's active country.

    Args:
        action: "create" or "list".
        name: task title (create only).
        lead_id: related lead id (optional).
        priority: task priority.
        due_date: due date string "YYYY-MM-DD".
        status: task status.
    """
    print(f"\n📋 [TOOL] MANAGE TASKS → action={action} | name={name!r} | lead_id={lead_id}")
    logger.info("Tool used: manage_tasks | action=%s", action)

    user = _get_user(config)
    if user is None:
        return json.dumps({"error": "not_authenticated"})

    from users.models import Task, Lead

    action = (action or "").strip().lower()

    # ── Create ────────────────────────────────────────────────────────────────
    if action == "create":
        if not (name or "").strip():
            return json.dumps({"error": "missing_name", "detail": "A task name is required."})

        norm_priority = _TASK_PRIORITY_MAP.get((priority or "medium").strip().lower(), "medium")
        norm_status = _TASK_STATUS_MAP.get((status or "todo").strip().lower(), "pending")

        related_lead = None
        if lead_id:
            # Role-aware access (superadmin sees all, company_admin company-wide, etc.)
            related_lead = _leads_for_user(user).filter(pk=lead_id).first()
            if related_lead is None:
                return json.dumps({
                    "error": "lead_not_found",
                    "detail": "That lead does not exist or you don't have access to it.",
                })

        task = Task.objects.create(
            name=name.strip(),
            priority=norm_priority,
            status=norm_status,
            due_date=_parse_due_date(due_date),
            associated_country=getattr(user, "current_country", "") or "",
            created_by=user,
            related_lead=related_lead,
        )

        return json.dumps({
            "success": True,
            "task": {
                "id": task.id,
                "name": task.name,
                "priority": task.priority,
                "status": task.status,
                "due_date": str(task.due_date) if task.due_date else None,
                "associated_country": task.associated_country,
                "lead_id": related_lead.id if related_lead else None,
                "lead_name": related_lead.name if related_lead else None,
            },
        }, indent=2)

    # ── List ────────────────────────────────────────────────────────────────
    if action == "list":
        # Role-aware task visibility (mirrors lead scoping):
        #   superadmin    → every task in the system
        #   company_admin → tasks by any user in their company
        #   team_manager  → tasks by themselves or their managed team members
        #   agent / other → only their own tasks
        role = getattr(user, "role", None)
        qs = Task.objects.select_related("related_lead", "created_by")

        if role == "superadmin":
            pass
        elif role in ("axiyon_admin", "company_admin"):
            qs = qs.filter(created_by__company=user.company)
        elif role == "team_manager":
            managed_team = getattr(user, "managed_team", None)
            if managed_team:
                qs = qs.filter(
                    Q(created_by=user) | Q(created_by__team=managed_team)
                ).distinct()
            else:
                qs = qs.filter(created_by=user)
        else:
            qs = qs.filter(created_by=user)

        tasks = [
            {
                "id": t.id,
                "name": t.name,
                "priority": t.priority,
                "status": t.status,
                "due_date": str(t.due_date) if t.due_date else None,
                "associated_country": t.associated_country,
                "lead_id": t.related_lead_id,
                "lead_name": t.related_lead.name if t.related_lead else None,
            }
            for t in qs[:50]
        ]

        if not tasks:
            return json.dumps({"count": 0, "results": [], "detail": "You have no tasks yet."})
        return json.dumps({"count": len(tasks), "results": tasks}, indent=2)

    return json.dumps({"error": "invalid_action", "detail": "action must be 'create' or 'list'."})


# ── Lead creation tool ────────────────────────────────────────────────────────

_LEAD_ROLES = {"axiyon_admin", "company_admin", "team_manager", "agent"}


def _parse_budget(value):
    """Parse a budget string/number into Decimal. Accepts things like '500,000',
    '£500k', '$1.2m', '2,50,000'. Returns None if uninterpretable."""
    if value is None:
        return None
    from decimal import Decimal, InvalidOperation
    if isinstance(value, (int, float, Decimal)):
        try:
            return Decimal(str(value))
        except InvalidOperation:
            return None

    import re
    text = str(value).strip().lower()
    if not text:
        return None

    multiplier = Decimal("1")
    if text.endswith("k"):
        multiplier = Decimal("1000")
        text = text[:-1]
    elif text.endswith("m") or text.endswith("mn"):
        multiplier = Decimal("1000000")
        text = text.rstrip("mn")
    elif text.endswith("bn") or text.endswith("b"):
        multiplier = Decimal("1000000000")
        text = text.rstrip("bn")

    # Strip currency symbols, spaces and commas.
    cleaned = re.sub(r"[^0-9.]", "", text)
    if not cleaned:
        return None
    try:
        return (Decimal(cleaned) * multiplier).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None


@tool
def create_lead(
    name: str,
    phone_no: str,
    estimated_budget: str = None,
    config: RunnableConfig = None,
) -> str:
    """
    Create a new sales lead for the current user. Only company_admin, team_manager
    and agent roles may use this.

    The lead's `desired_country` is automatically set to the current user's active
    country — NEVER ask the user for it.

    Ask the user ONLY for these three fields:
      - name:              the lead's full name.
      - phone_no:          the lead's phone number.
      - estimated_budget:  the lead's budget (a number; suffixes like 'k'/'m' are ok).

    Do NOT ask about email, source, category, type, project, status or any other
    field — they are left blank by default.

    Args:
        name: lead full name (required).
        phone_no: lead phone number (required).
        estimated_budget: lead budget as a number or string like "500000", "500k",
            "£1.2m" (required).
    """
    print(f"\n👤 [TOOL] CREATE LEAD → name={name!r} | phone={phone_no!r} | budget={estimated_budget!r}")
    logger.info("Tool used: create_lead | name=%s", name)

    role = (config or {}).get("configurable", {}).get("user_role", "") or ""
    if role not in _LEAD_ROLES:
        return json.dumps({
            "error": "permission_denied",
            "detail": "Only company_admin, team_manager and agent roles can create leads.",
        })

    user = _get_user(config)
    if user is None:
        return json.dumps({"error": "not_authenticated"})

    missing = []
    if not (name or "").strip():
        missing.append("name")
    if not (phone_no or "").strip():
        missing.append("phone_no")
    budget_value = _parse_budget(estimated_budget)
    if budget_value is None:
        missing.append("estimated_budget")
    if missing:
        return json.dumps({
            "error": "missing_fields",
            "missing": missing,
            "detail": "name, phone_no and estimated_budget are all required.",
        })

    from users.models import Lead

    desired_country = getattr(user, "current_country", "") or ""

    lead = Lead.objects.create(
        name=name.strip(),
        phone_no=phone_no.strip(),
        estimated_budget=budget_value,
        desired_country=desired_country,
        created_by=user,
    )

    return json.dumps({
        "success": True,
        "lead": {
            "id": lead.id,
            "name": lead.name,
            "phone_no": lead.phone_no,
            "estimated_budget": float(lead.estimated_budget),
            "desired_country": lead.desired_country,
            "status": lead.status,
        },
    }, indent=2)


# ── Organisation tools (companies + users) ───────────────────────────────────


@tool
def list_companies(config: RunnableConfig = None) -> str:
    """
    List all companies in the system. Only the **superadmin** role may use this tool.

    Returns each company's id, name, operating countries and the number of users
    that belong to it.
    """
    print("\n🏢 [TOOL] LIST COMPANIES")
    logger.info("Tool used: list_companies")

    role = (config or {}).get("configurable", {}).get("user_role", "") or ""
    if role != "superadmin":
        return json.dumps({
            "error": "permission_denied",
            "detail": "Only the superadmin role can list companies.",
        })

    from users.models import Company

    companies = []
    for c in Company.objects.all().prefetch_related("users"):
        companies.append({
            "id": c.id,
            "name": c.name,
            "operating_countries": c.operating_countries or [],
            "users_count": c.users.count(),
        })

    if not companies:
        return json.dumps({"count": 0, "results": []})
    return json.dumps({"count": len(companies), "results": companies}, indent=2)


def _serialize_user(u):
    return {
        "id": u.id,
        "email": u.email,
        "full_name": u.full_name,
        "role": u.role,
        "status": u.status,
        "current_country": u.current_country,
        "company_id": u.company_id,
        "company_name": u.company.name if u.company_id and u.company else None,
        "team_id": u.team_id,
        "team_name": u.team.name if u.team_id and u.team else None,
    }


@tool
def list_users(role_filter: str = None, config: RunnableConfig = None) -> str:
    """
    List users the current logged-in user is allowed to see, scoped by role:

    - **superadmin**    → every user in the system (across all companies).
    - **company_admin** → every user in their own company (all roles).
    - **team_manager**  → the agents in the team they manage.
    - **agent**         → only themselves.

    Args:
        role_filter: optional. If given, further restrict to users of that role
            (e.g. "agent", "team_manager", "company_admin", "superadmin").
    """
    print(f"\n👥 [TOOL] LIST USERS → role_filter={role_filter!r}")
    logger.info("Tool used: list_users | role_filter=%s", role_filter)

    user = _get_user(config)
    if user is None:
        return json.dumps({"error": "not_authenticated"})

    from django.contrib.auth import get_user_model
    User = get_user_model()

    role = getattr(user, "role", "") or ""
    qs = User.objects.select_related("company", "team")

    if role == "superadmin":
        pass
    elif role in ("axiyon_admin", "company_admin"):
        qs = qs.filter(company=user.company)
    elif role == "team_manager":
        managed_team = getattr(user, "managed_team", None)
        if managed_team is None:
            return json.dumps({
                "count": 0,
                "results": [],
                "detail": "You do not manage any team yet.",
            })
        qs = qs.filter(team=managed_team, role=User.Role.AGENT)
    else:
        qs = qs.filter(pk=user.pk)

    if role_filter:
        qs = qs.filter(role=role_filter.strip().lower())

    users = [_serialize_user(u) for u in qs.order_by("role", "email")[:100]]
    if not users:
        return json.dumps({"count": 0, "results": [], "detail": "No users found."})
    return json.dumps({"count": len(users), "results": users}, indent=2)


# ── Call History & Call Information Tool ──────────────────────────────────────

@tool
def query_call_history(
    lead_id: int = None,
    lead_name: str = None,
    phone_number: str = None,
    call_status: str = None,
    limit: int = 10,
    config: RunnableConfig = None,
) -> str:
    """
    Query outbound AI call history and analytics for leads. Use this tool whenever the user asks:
    - Whether a lead was called or not ("Did we call lead X?", "Has John been called?")
    - Whether the lead answered the call ("Did the lead answer?", "Was the call answered?")
    - Call outcomes, call duration, call summary, key sentiments, or detected intents ("What was discussed in the call?", "What were the key sentiments / detected intents of the call?")
    - List recent call logs or check call details for a specific lead or phone number.

    Args:
        lead_id: optional lead ID to filter call history for a specific lead.
        lead_name: optional lead name (case-insensitive substring match).
        phone_number: optional phone number (case-insensitive substring match).
        call_status: optional call status filter (e.g. "completed", "no_answer", "busy", "failed", "in_progress", "scheduled", "cancelled").
        limit: max call records to return (default 10).
    """
    print(f"\n[CALL TOOL] QUERY CALL HISTORY -> lead_id={lead_id} | lead_name={lead_name!r} | status={call_status!r}")
    logger.info("Tool used: query_call_history | lead_id=%s | lead_name=%s", lead_id, lead_name)

    user = _get_user(config)
    if user is None:
        return json.dumps({"error": "not_authenticated"})

    from calling_agent.services import calls_visible_to_user
    from calling_agent.models import Call
    from django.db.models import Q

    qs = calls_visible_to_user(user).select_related("lead", "company", "context_user")

    if lead_id is not None:
        qs = qs.filter(lead_id=lead_id)

    if lead_name:
        name_str = lead_name.strip()
        qs = qs.filter(Q(lead_name__icontains=name_str) | Q(lead__name__icontains=name_str))

    if phone_number:
        phone_str = phone_number.strip()
        qs = qs.filter(Q(phone_number__icontains=phone_str) | Q(lead__phone_no__icontains=phone_str))

    if call_status:
        status_str = call_status.strip().lower()
        qs = qs.filter(status=status_str)

    qs = qs.order_by("-created_at")
    total_count = qs.count()

    if total_count == 0:
        return json.dumps({
            "total_count": 0,
            "count": 0,
            "results": [],
            "detail": "No call records found matching the given criteria."
        })

    records = []
    for call in qs[:limit]:
        lead_obj = call.lead
        records.append({
            "id": call.id,
            "public_id": str(call.public_id),
            "lead_id": call.lead_id,
            "lead_name": (lead_obj.name if lead_obj and lead_obj.name else call.lead_name) or "Unknown",
            "phone_number": call.phone_number or (lead_obj.phone_no if lead_obj else ""),
            "lead_status": lead_obj.get_status_display() if lead_obj else None,
            "country": lead_obj.country if lead_obj else None,
            "direction": call.direction,
            "trigger": call.trigger,
            "status": call.status,
            "status_display": call.get_status_display(),
            "answered": call.status == Call.Status.COMPLETED or bool(call.answered_at),
            "duration": call.duration,
            "duration_seconds": call.duration_seconds,
            "failure_code": call.failure_code or None,
            "failure_detail": call.failure_detail or None,
            "summary": call.summary or "No summary recorded.",
            "key_sentiments": call.key_sentiments or [],
            "detected_intents": call.detected_intents or [],
            "scheduled_for": call.scheduled_for.isoformat() if call.scheduled_for else None,
            "initiated_at": call.initiated_at.isoformat() if call.initiated_at else None,
            "answered_at": call.answered_at.isoformat() if call.answered_at else None,
            "ended_at": call.ended_at.isoformat() if call.ended_at else None,
            "created_at": call.created_at.isoformat() if call.created_at else None,
        })

    return json.dumps({
        "total_count": total_count,
        "count": len(records),
        "results": records,
    }, indent=2)


# All tools exported for the agent
ALL_TOOLS = [
    query_database,
    query_call_history,
    list_my_leads,
    list_lead_projects,
    list_project_units,
    generate_proposal,
    send_proposal_to_lead,
    search_knowledge_base,
    manage_tasks,
    create_lead,
    list_companies,
    list_users,
]
