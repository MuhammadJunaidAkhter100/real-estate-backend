from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor, TimeoutError, as_completed
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.db.models import (
    Case,
    DecimalField,
    Exists,
    IntegerField,
    OuterRef,
    Q,
    QuerySet,
    Value,
    When,
)
from django.db.models.functions import Coalesce
from django.utils import timezone

from calling_agent.discount_utils import (
    build_project_promotion_block,
    compute_budget_fit,
    lowest_available_unit_prices_for_projects,
    proposal_summary_snippet,
    promotion_status_is_active,
    unit_pricing_block,
)
from calling_agent.exceptions import (
    ToolContextUnavailableError,
    ToolServiceUnavailableError,
)
from calling_agent.models import Call
from chatbot.pinecone_service import PineconeService, PineconeServiceError
from new_proposal.models import GeneratedProposal
from notifications.services import send_notification
from projects.models import Project, Unit
from users.models import Lead
from whatsapp.models import WhatsAppAccount
from whatsapp.services import WhatsAppService

logger = logging.getLogger(__name__)

_KNOWLEDGE_EXECUTOR = ThreadPoolExecutor(
    max_workers=4,
    thread_name_prefix='calling-knowledge',
)

_LOCATION_SPLIT_RE = re.compile(r'[,\s]+')
_BEDROOM_CATEGORY_RE = re.compile(
    r'^(\d+)[\s_-]*bed(?:room)?s?$',
    re.IGNORECASE,
)
_STUDIO_CATEGORY_RE = re.compile(r'^studio$', re.IGNORECASE)
_BEDROOM_CATEGORY_VARIANTS = (
    '{count} Bed',
    '{count}_bed',
    '{count}-bed',
    '{count} bedroom',
    '{count}_bedroom',
    '{count}-bedroom',
)

_BEDROOM_COUNT_FIELD = {
    0: 'studio',
    1: 'bed_1',
    2: 'bed_2',
    3: 'bed_3',
}

_PROJECT_SEARCH_VALUES = (
    'id',
    'title',
    'location',
    'associated_country',
    'starting_price',
    'yield_percentage',
    'currency',
    'project_type',
    'property_category',
    'bed_1',
    'bed_2',
    'bed_3',
    'studio',
)

_ASSIGNED_PROJECT_VALUES = (
    'id',
    'title',
    'location',
    'associated_country',
    'starting_price',
    'currency',
    'project_type',
    'property_category',
)

_PROJECT_CONTEXT_VALUES = (
    'id',
    'title',
    'description',
    'location',
    'developer',
    'estimated_completion',
    'status',
    'starting_price',
    'yield_percentage',
    'currency',
    'project_type',
    'property_category',
)


def _decimal_to_string(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def _compact_text(value: str, max_length: int) -> str:
    return ' '.join((value or '').split())[:max_length]


def _location_search_terms(location: str) -> list[str]:
    return [term for term in _LOCATION_SPLIT_RE.split(location.strip()) if term]


def _unit_category_query(category: str) -> Q:
    text = category.strip()
    if not text:
        return Q()
    bedroom_match = _BEDROOM_CATEGORY_RE.fullmatch(text)
    if bedroom_match:
        count = bedroom_match.group(1)
        query = Q()
        for template in _BEDROOM_CATEGORY_VARIANTS:
            query |= Q(category__icontains=template.format(count=count))
        return query
    if _STUDIO_CATEGORY_RE.fullmatch(text):
        return Q(category__icontains='studio')
    return Q(category__icontains=text)


def _lead_for_call(call: Call) -> Lead:
    lead = call.lead
    if (
        lead is None
        or lead.created_by.company_id is None
        or lead.created_by.company_id != call.company_id
    ):
        raise ToolContextUnavailableError(
            'The call no longer has valid lead context.'
        )
    return lead


def _lead_assigned_project_ids(lead: Lead) -> set[int]:
    if (
        hasattr(lead, '_prefetched_objects_cache')
        and 'projects' in lead._prefetched_objects_cache
    ):
        project_ids = {project.pk for project in lead.projects.all()}
    else:
        project_ids = set(lead.projects.values_list('pk', flat=True))
    if lead.project_id:
        project_ids.add(lead.project_id)
    return project_ids


def eligible_projects_for_call(call: Call) -> QuerySet[Project]:
    """LIVE projects not hidden from the call's company (discovery catalog)."""
    _lead_for_call(call)
    return (
        Project.objects.filter(
            project_status=Project.ProjectStatus.LIVE,
        )
        .exclude(visible_to_companies=call.company)
        .distinct()
    )


def linked_projects_for_call(call: Call) -> QuerySet[Project]:
    lead = _lead_for_call(call)
    project_ids = _lead_assigned_project_ids(lead)
    if not project_ids:
        return eligible_projects_for_call(call)

    return (
        Project.objects.filter(
            pk__in=project_ids,
            project_status=Project.ProjectStatus.LIVE,
        )
        .exclude(visible_to_companies=call.company)
        .distinct()
    )


def assigned_projects_for_call(call: Call) -> QuerySet[Project]:
    """LIVE projects explicitly assigned to the call's lead (no catalog fallback)."""
    lead = _lead_for_call(call)
    project_ids = _lead_assigned_project_ids(lead)
    if not project_ids:
        return Project.objects.none()

    return (
        Project.objects.filter(
            pk__in=project_ids,
            project_status=Project.ProjectStatus.LIVE,
        )
        .exclude(visible_to_companies=call.company)
        .distinct()
    )


class LeadContextToolService:
    def get_context(self, call: Call) -> dict[str, Any]:
        lead = _lead_for_call(call)
        specialist = lead.assigned_to or call.context_user
        specialist_name = ''
        if specialist is not None:
            specialist_name = specialist.full_name.strip()

        assigned_qs = list(
            assigned_projects_for_call(call)
            .prefetch_related('promotions')
            .order_by('title')
        )
        lowest_prices = lowest_available_unit_prices_for_projects(
            [project.pk for project in assigned_qs]
        )
        assigned_projects = []
        for project in assigned_qs:
            row = {
                field: getattr(project, field)
                for field in _ASSIGNED_PROJECT_VALUES
            }
            lowest_price = lowest_prices.get(project.pk)
            budget_fit = compute_budget_fit(lead.estimated_budget, lowest_price)
            promotion = build_project_promotion_block(project)
            assigned_projects.append(
                {
                    'id': row['id'],
                    'title': row['title'],
                    'location': row['location'],
                    'country': row['associated_country'],
                    'starting_price': _decimal_to_string(row['starting_price']),
                    'lowest_available_unit_price': _decimal_to_string(
                        lowest_price
                    ),
                    'budget_fit': budget_fit,
                    'currency': row['currency'],
                    'project_type': row['project_type'],
                    'property_category': row['property_category'],
                    'promotion': promotion,
                }
            )

        latest_proposal = (
            GeneratedProposal.objects.filter(
                lead_id=lead.pk,
                status=GeneratedProposal.Status.COMPLETED,
            )
            .select_related('project', 'unit')
            .order_by('-created_at')
            .first()
        )
        latest_proposal_data = None
        if latest_proposal is not None:
            latest_proposal_data = {
                'id': latest_proposal.pk,
                'project_title': latest_proposal.project.title,
                'unit_label': latest_proposal.unit.label,
                'hosted_url': latest_proposal.hosted_url or None,
                'sent_at': latest_proposal.created_at.isoformat(),
                'summary_snippet': proposal_summary_snippet(
                    latest_proposal.ai_facts,
                ),
            }

        logger.info(
            'lead_context call_id=%s lead_id=%s assigned_projects=%d '
            'has_proposal=%s',
            call.public_id,
            lead.pk,
            len(assigned_projects),
            latest_proposal_data is not None,
        )

        return {
            'lead': {
                'name': lead.name,
                'phone_number': lead.phone_no,
                'status': lead.status,
                'stage': lead.stage,
                'do_not_contact': lead.do_not_contact,
                'source': lead.source,
                'country': lead.country,
                'desired_country': lead.desired_country,
                'desired_location': lead.desired_location,
                'estimated_budget': _decimal_to_string(
                    lead.estimated_budget
                ),
                'category': lead.category,
                'property_type': lead.type,
                'other_property_type': lead.other_type,
            },
            'assigned_specialist': {
                'name': specialist_name,
            }
            if specialist_name
            else None,
            'assigned_projects': assigned_projects,
            'assigned_projects_count': len(assigned_projects),
            'latest_proposal': latest_proposal_data,
        }


class ProjectContextToolService:
    def get_context(self, call: Call) -> dict[str, Any]:
        projects = list(
            linked_projects_for_call(call)
            .prefetch_related('promotions')
            .order_by('title')
        )
        data = []
        for project in projects:
            row = {
                field: getattr(project, field)
                for field in _PROJECT_CONTEXT_VALUES
            }
            promotion = build_project_promotion_block(project)
            data.append(
                {
                    'id': row['id'],
                    'title': row['title'],
                    'description': _compact_text(row['description'] or '', 1000),
                    'location': row['location'],
                    'developer': row['developer'],
                    'estimated_completion': row['estimated_completion'],
                    'status': row['status'],
                    'starting_price': _decimal_to_string(row['starting_price']),
                    'yield_percentage': _decimal_to_string(
                        row['yield_percentage']
                    ),
                    'yield_basis': 'estimated',
                    'currency': row['currency'],
                    'project_type': row['project_type'],
                    'property_category': row['property_category'],
                    'promotion': promotion,
                }
            )
        return {'projects': data, 'count': len(data)}


class ProjectSearchToolService:
    def search(
        self,
        call: Call,
        filters: dict[str, Any],
    ) -> dict[str, Any]:
        available_units = Unit.objects.filter(
            project_id=OuterRef('pk'),
            status=Unit.UnitStatus.AVAILABLE,
        )
        qs = eligible_projects_for_call(call).filter(Exists(available_units))

        country = (filters.get('country') or '').strip()
        location = (filters.get('location') or '').strip()
        project_name = (filters.get('project_name') or '').strip()
        project_type = filters.get('project_type')
        property_category = (filters.get('property_category') or '').strip()
        bedrooms = filters.get('bedrooms')
        min_budget = filters.get('min_budget')
        max_budget = filters.get('max_budget')
        limit = filters.get('limit', 5)

        location_terms = _location_search_terms(location) if location else []

        if country:
            qs = qs.filter(associated_country__icontains=country)
        if location_terms:
            location_q = Q()
            for term in location_terms:
                location_q |= Q(location__icontains=term)
            qs = qs.filter(location_q)
        if project_name:
            qs = qs.filter(title__icontains=project_name)
        if project_type:
            qs = qs.filter(project_type=project_type)
        if property_category:
            qs = qs.filter(property_category__icontains=property_category)
        if bedrooms is not None:
            bedroom_field = _BEDROOM_COUNT_FIELD[int(bedrooms)]
            qs = qs.filter(**{f'{bedroom_field}__gt': 0})
        if min_budget is not None:
            qs = qs.filter(starting_price__gte=min_budget)
        if max_budget is not None:
            qs = qs.filter(starting_price__lte=max_budget)

        annotations: dict[str, Any] = {}
        order_fields: list[str] = []
        value_fields = list(_PROJECT_SEARCH_VALUES)

        if project_name:
            annotations['rank_name'] = Case(
                When(title__iexact=project_name, then=Value(0)),
                default=Value(1),
                output_field=IntegerField(),
            )
            order_fields.append('rank_name')
            value_fields.append('rank_name')

        if location_terms:
            exact_location = Q()
            for term in location_terms:
                exact_location |= Q(location__iexact=term)
            annotations['rank_location'] = Case(
                When(exact_location, then=Value(0)),
                default=Value(1),
                output_field=IntegerField(),
            )
            order_fields.append('rank_location')
            value_fields.append('rank_location')

        if country:
            annotations['rank_country'] = Case(
                When(associated_country__iexact=country, then=Value(0)),
                default=Value(1),
                output_field=IntegerField(),
            )
            order_fields.append('rank_country')
            value_fields.append('rank_country')

        order_fields.extend(['starting_price', 'id'])
        if annotations:
            qs = qs.annotate(**annotations)

        projects = list(
            qs.prefetch_related('promotions').order_by(*order_fields)[:limit]
        )
        data = []
        for project in projects:
            promotion = build_project_promotion_block(project)
            data.append(
                {
                    'id': project.pk,
                    'title': project.title,
                    'location': project.location,
                    'country': project.associated_country,
                    'starting_price': _decimal_to_string(project.starting_price),
                    'yield_percentage': _decimal_to_string(
                        project.yield_percentage
                    ),
                    'yield_basis': 'estimated',
                    'currency': project.currency,
                    'project_type': project.project_type,
                    'property_category': project.property_category,
                    'bed_1': project.bed_1 or 0,
                    'bed_2': project.bed_2 or 0,
                    'bed_3': project.bed_3 or 0,
                    'studio': project.studio or 0,
                    'promotion': promotion,
                }
            )
        return {'projects': data, 'count': len(data)}


class UnitSearchToolService:
    def search(
        self,
        call: Call,
        filters: dict[str, Any],
    ) -> dict[str, Any]:
        requested_project_id = filters.get('project_id')
        if requested_project_id is not None:
            # Existence check avoids loading the full eligible catalog into Python.
            if not (
                eligible_projects_for_call(call)
                .filter(pk=requested_project_id)
                .exists()
            ):
                return {'units': [], 'count': 0}
            units = Unit.objects.select_related('project').filter(
                project_id=requested_project_id
            )
        else:
            linked = linked_projects_for_call(call)
            units = Unit.objects.select_related('project').filter(
                project_id__in=linked.values('pk')
            )

        units = units.prefetch_related('project__promotions')

        status_filter = filters.get('status')
        if status_filter:
            units = units.filter(status=status_filter)

        unit_id = filters.get('unit_id')
        if unit_id:
            units = units.filter(pk=unit_id)

        label_filter = (
            filters.get('unit_label')
            or filters.get('label')
            or filters.get('search_query')
            or filters.get('unit_name')
        )
        if label_filter:
            units = units.filter(
                Q(label__icontains=label_filter)
                | Q(category__icontains=label_filter)
            )

        effective_price_field = DecimalField(
            max_digits=12,
            decimal_places=2,
        )
        units = units.annotate(
            effective_price=Coalesce(
                'discounted_price',
                'list_price',
                output_field=effective_price_field,
            )
        )

        category = filters.get('category')
        if category:
            units = units.filter(_unit_category_query(category))
        if filters.get('min_budget') is not None:
            units = units.filter(
                effective_price__gte=filters['min_budget']
            )
        if filters.get('max_budget') is not None:
            units = units.filter(
                effective_price__lte=filters['max_budget']
            )
        if filters.get('min_area_m2') is not None:
            units = units.filter(area_m2__gte=filters['min_area_m2'])
        if filters.get('max_area_m2') is not None:
            units = units.filter(area_m2__lte=filters['max_area_m2'])

        unit_rows = list(
            units.order_by('effective_price', 'label')[: filters.get('limit', 5)]
        )
        promotion_cache: dict[int, dict[str, Any]] = {}
        data = []
        for unit in unit_rows:
            project_id = unit.project_id
            promotion = promotion_cache.get(project_id)
            if promotion is None:
                promotion = build_project_promotion_block(unit.project)
                promotion_cache[project_id] = promotion
            pricing = unit_pricing_block(
                list_price=unit.list_price,
                discounted_price=unit.discounted_price,
                effective_price=unit.effective_price,
                has_active_promotion=promotion_status_is_active(
                    promotion['status']
                ),
            )
            data.append(
                {
                    'id': unit.pk,
                    'project_id': unit.project_id,
                    'project_title': unit.project.title,
                    'label': unit.label,
                    'category': unit.category,
                    'floor': unit.floor,
                    'area_m2': _decimal_to_string(unit.area_m2),
                    'area_ft2': _decimal_to_string(unit.area_ft2),
                    'price': _decimal_to_string(unit.effective_price),
                    'list_price': pricing['list_price'],
                    'discounted_price': pricing['discounted_price'],
                    'effective_price': pricing['effective_price'],
                    'has_discount': pricing['has_discount'],
                    'pricing': pricing,
                    'promotion': promotion,
                    'est_yield_gross': _decimal_to_string(unit.est_yield_gross),
                    'yield_basis': (
                        'estimated'
                        if unit.est_yield_gross is not None
                        else None
                    ),
                    'currency': unit.currency or unit.project.currency,
                    'status': unit.status,
                }
            )
        return {'units': data, 'count': len(data)}


_PAYMENT_QUERY_KEYWORDS = (
    'payment',
    'deposit',
    'installment',
    'instalment',
    'finance',
    'mortgage',
    'booking amount',
    'payment plan',
)
_PAYMENT_CHUNK_KEYWORDS = _PAYMENT_QUERY_KEYWORDS + ('plan',)
_COMPLETION_QUERY_KEYWORDS = (
    'completion',
    'complete',
    'completed',
    'ready',
    'handover',
    'occupancy',
    'possession',
)
_COMPLETION_CHUNK_KEYWORDS = _COMPLETION_QUERY_KEYWORDS + ('available',)
_COMPLETION_QUERY_PHRASES = (
    'when can i view',
    'when can i see',
    'when will it be ready',
    'when is it ready',
)
_FLOOR_PLAN_SOURCE_MARKERS = (
    'floor_plan',
    'floor plans',
    'floor plan',
    'floorplan',
    'floor-plan',
)
_QUARTER_PATTERN = re.compile(r'\bq[1-4]\b', re.IGNORECASE)


def _normalize_knowledge_text(value: str) -> str:
    return re.sub(r'\s+', ' ', value.lower()).strip()


def _text_has_keyword(haystack: str, keywords: tuple[str, ...]) -> bool:
    for keyword in keywords:
        if ' ' in keyword:
            if keyword in haystack:
                return True
            continue
        if re.search(rf'\b{re.escape(keyword)}\b', haystack):
            return True
    return False


def _knowledge_query_intents(query: str) -> set[str]:
    normalized = _normalize_knowledge_text(query)
    intents: set[str] = set()
    if _text_has_keyword(normalized, _PAYMENT_QUERY_KEYWORDS):
        intents.add('payment')
    if (
        _text_has_keyword(normalized, _COMPLETION_QUERY_KEYWORDS)
        or any(phrase in normalized for phrase in _COMPLETION_QUERY_PHRASES)
        or _QUARTER_PATTERN.search(normalized)
    ):
        intents.add('completion')
    return intents


def _match_source_and_text(match: dict[str, Any]) -> tuple[str, str]:
    metadata = match.get('metadata') or {}
    source = _normalize_knowledge_text(
        str(
            metadata.get('original_filename')
            or metadata.get('project_title')
            or metadata.get('label')
            or ''
        )
    )
    text = _normalize_knowledge_text(str(match.get('text') or ''))
    return source, text


def _is_floor_plan_source(source: str) -> bool:
    return any(marker in source for marker in _FLOOR_PLAN_SOURCE_MARKERS)


def _chunk_matches_intent(source: str, text: str, intent: str) -> bool:
    combined = f'{source} {text}'
    if intent == 'payment':
        return _text_has_keyword(combined, _PAYMENT_CHUNK_KEYWORDS)
    if intent == 'completion':
        return (
            _text_has_keyword(combined, _COMPLETION_CHUNK_KEYWORDS)
            or _QUARTER_PATTERN.search(combined) is not None
        )
    return False


def _filter_matches_for_knowledge_intents(
    matches: list[dict[str, Any]],
    intents: set[str],
) -> list[dict[str, Any]]:
    filtered: list[dict[str, Any]] = []
    for match in matches:
        source, text = _match_source_and_text(match)
        if _is_floor_plan_source(source):
            continue
        if any(
            _chunk_matches_intent(source, text, intent)
            for intent in intents
        ):
            filtered.append(match)
    return filtered


class KnowledgeSearchToolService:
    def __init__(self, pinecone_service: PineconeService | None = None) -> None:
        self.pinecone_service = pinecone_service

    def search(
        self,
        call: Call,
        *,
        query: str,
        top_k: int,
        project_id: int | None = None,
    ) -> dict[str, Any]:
        _lead_for_call(call)
        if project_id is not None:
            # Same eligibility rule as unit_search: explicit id may target any
            # discoverable live project, not only projects already linked to the lead.
            if not (
                eligible_projects_for_call(call)
                .filter(pk=project_id)
                .exists()
            ):
                return self._empty_result()
            project_ids = [project_id]
        else:
            project_ids = list(
                linked_projects_for_call(call).values_list('pk', flat=True)
            )
        metadata_filters: list[dict[str, Any]] = []
        project_document_filter = (
            {
                'source': {'$eq': 'project_document'},
                'project_id': {'$in': project_ids},
            }
            if project_ids
            else None
        )
        if project_id is not None:
            # Agent already scoped the project. Skip the personal user_id
            # index so a live call does not pay for two OpenAI embeds.
            if project_document_filter is None:
                return self._empty_result()
            metadata_filters.append(project_document_filter)
        else:
            if call.context_user_id:
                metadata_filters.append({'user_id': call.context_user_id})
            if project_document_filter is not None:
                metadata_filters.append(project_document_filter)
        if not metadata_filters:
            return self._empty_result()

        service = self.pinecone_service or PineconeService()
        futures = [
            _KNOWLEDGE_EXECUTOR.submit(
                service.search,
                query,
                top_k=top_k,
                metadata_filter=metadata_filter,
            )
            for metadata_filter in metadata_filters
        ]
        matches: list[dict[str, Any]] = []
        try:
            for future in as_completed(
                futures,
                timeout=settings.ELEVENLABS_KNOWLEDGE_TIMEOUT_SECONDS,
            ):
                matches.extend(future.result())
        except TimeoutError:
            for future in futures:
                future.cancel()
            logger.warning(
                'ElevenLabs knowledge search timed out call_id=%s',
                call.public_id,
            )
            return self._empty_result()
        except PineconeServiceError as exc:
            for future in futures:
                future.cancel()
            logger.warning(
                'Pinecone knowledge search failed call_id=%s',
                call.public_id,
            )
            raise ToolServiceUnavailableError(
                'Knowledge search is unavailable.'
            ) from exc
        except Exception as exc:
            for future in futures:
                future.cancel()
            logger.exception(
                'Unexpected ElevenLabs knowledge search failure call_id=%s',
                call.public_id,
            )
            raise ToolServiceUnavailableError(
                'Knowledge search is unavailable.'
            ) from exc

        threshold = settings.ELEVENLABS_KNOWLEDGE_SCORE_THRESHOLD
        strong_matches = [
            match
            for match in matches
            if (match.get('score') or 0) >= threshold
        ]
        strong_matches.sort(
            key=lambda match: match.get('score') or 0,
            reverse=True,
        )
        intents = _knowledge_query_intents(query)
        if intents:
            strong_matches = _filter_matches_for_knowledge_intents(
                strong_matches,
                intents,
            )

        results: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for match in strong_matches:
            metadata = match.get('metadata') or {}
            source = (
                metadata.get('original_filename')
                or metadata.get('project_title')
                or metadata.get('label')
                or 'Knowledge base'
            )
            text = _compact_text(str(match.get('text') or ''), 900)
            deduplication_key = (text, str(source))
            if not text or deduplication_key in seen:
                continue
            seen.add(deduplication_key)
            results.append(
                {
                    'text': text,
                    'source': _compact_text(str(source), 255),
                    'score': round(float(match.get('score') or 0), 3),
                }
            )
            if len(results) == top_k:
                break

        if not results:
            return self._empty_result()
        return {
            'answerable': True,
            'count': len(results),
            'results': results,
        }

    @staticmethod
    def _empty_result() -> dict[str, Any]:
        return {
            'answerable': False,
            'count': 0,
            'results': [],
            'message': (
                'The answer is not available in the authorized knowledge base.'
            ),
        }


class RequestProposalToolService:
    def request_proposal(
        self,
        call: Call,
        *,
        project_id: int | None = None,
        unit_id: int | None = None,
        reason: str = '',
    ) -> dict[str, Any]:
        lead = _lead_for_call(call)
        proposals_qs = GeneratedProposal.objects.filter(
            lead_id=lead.pk,
            status=GeneratedProposal.Status.COMPLETED,
        )
        if project_id:
            proposals_qs = proposals_qs.filter(project_id=project_id)
        if unit_id:
            proposals_qs = proposals_qs.filter(unit_id=unit_id)

        proposal = (
            proposals_qs.only(
                'id',
                'hosted_url',
                'file',
                'created_at',
            )
            .order_by('-created_at')
            .first()
        )

        if proposal:
            proposal_url = proposal.hosted_url
            if not proposal_url and proposal.file:
                backend_base = getattr(settings, 'BACKEND_URL', '').rstrip('/')
                proposal_url = f'{backend_base}{proposal.file.url}'

            whatsapp_sent = False
            has_wa_account = WhatsAppAccount.objects.filter(
                user_id=lead.created_by_id,
            ).exists()
            if has_wa_account and lead.phone_no:
                try:
                    clean_phone = (
                        lead.phone_no.strip('+')
                        .replace(' ', '')
                        .replace('-', '')
                    )
                    chat_id = f'{clean_phone}@c.us'
                    msg_text = (
                        f'Hello {lead.name}, here is the project proposal '
                        f'you requested: {proposal_url}'
                    )
                    WhatsAppService().send_text_message(
                        user=lead.created_by,
                        payload={'chat_id': chat_id, 'text': msg_text},
                    )
                    whatsapp_sent = True
                except Exception as exc:
                    logger.warning(
                        'Failed to send WhatsApp proposal link to lead %s: %s',
                        lead.pk,
                        exc,
                    )

            return {
                'proposal_exists': True,
                'whatsapp_sent': whatsapp_sent,
                'proposal_url': proposal_url,
                'task_created': False,
                'message': (
                    f'Existing proposal found for {lead.name}. '
                    + (
                        'Proposal link sent to lead WhatsApp.'
                        if whatsapp_sent
                        else 'Proposal link ready.'
                    )
                ),
            }

        from calling_agent.action_services import TaskActionService

        _action, task, created = TaskActionService().create_follow_up_task(
            call=call,
            title=f'Generate & send proposal for {lead.name}',
            priority='high',
            due_date=None,
            reason=reason or 'Lead requested project proposal on call.',
        )

        specialist = lead.assigned_to or lead.created_by or call.context_user
        if specialist:
            try:
                send_notification(
                    recipient=specialist,
                    type='task_assigned',
                    title=f'Proposal Needed: {lead.name}',
                    message=(
                        f"Lead '{lead.name}' requested a proposal on call. "
                        f'A task (#{task.pk}) has been created for you.'
                    ),
                    data={
                        'lead_id': lead.pk,
                        'task_id': task.pk,
                        'call_id': call.pk,
                    },
                )
            except Exception as exc:
                logger.warning(
                    'Failed to dispatch notification for proposal task: %s',
                    exc,
                )

        return {
            'proposal_exists': False,
            'whatsapp_sent': False,
            'task_created': created,
            'task_id': task.pk,
            'message': (
                f'No existing proposal found. Task #{task.pk} created for '
                f'team specialist to generate and send the proposal to '
                f'{lead.name}.'
            ),
        }
