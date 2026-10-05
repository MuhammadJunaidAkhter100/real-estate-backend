from __future__ import annotations

from django.utils import timezone

from calling_agent.discount_utils import (
    compute_budget_fit,
    lowest_available_unit_price,
)
from calling_agent.exceptions import ToolContextUnavailableError
from calling_agent.models import Call
from calling_agent.tool_services import assigned_projects_for_call, linked_projects_for_call
from new_proposal.models import GeneratedProposal

PRIOR_CALL_SUMMARY_MAX_LENGTH = 500

_CALL_OBJECTIVE_BY_TRIGGER = {
    Call.Trigger.MANUAL: 'Manual outbound call',
    Call.Trigger.SCHEDULED: 'Scheduled outbound call',
}


class OutboundDynamicVariablesBuilder:
    """Build safe ElevenLabs briefing variables from Call + Lead context."""

    @classmethod
    def build(cls, call: Call) -> dict[str, str]:
        lead = call.lead
        latest_proposal = cls._latest_proposal(call)
        return {
            'today_date': timezone.localdate().isoformat(),
            'lead_status': cls._text(getattr(lead, 'status', None)),
            'desired_country': cls._text(
                getattr(lead, 'desired_country', None)
            ),
            'desired_location': cls._text(
                getattr(lead, 'desired_location', None)
            ),
            'estimated_budget': cls._budget(lead),
            'category': cls._text(getattr(lead, 'category', None)),
            'property_type': cls._text(getattr(lead, 'type', None)),
            'assigned_specialist_name': cls._specialist_name(call),
            'linked_project_titles': cls._linked_project_titles(call),
            'prior_call_summary': cls._prior_call_summary(call),
            'call_objective': _CALL_OBJECTIVE_BY_TRIGGER.get(
                call.trigger,
                '',
            ),
            'has_sent_proposal': (
                'true' if latest_proposal is not None else 'false'
            ),
            'proposal_project_title': cls._proposal_project_title(
                latest_proposal
            ),
            'budget_fit_summary': cls._budget_fit_summary(call),
        }

    @staticmethod
    def _text(value: object | None) -> str:
        if value is None:
            return ''
        return str(value).strip()

    @classmethod
    def _budget(cls, lead: object | None) -> str:
        if lead is None:
            return ''
        budget = getattr(lead, 'estimated_budget', None)
        if budget is None:
            return ''
        return str(budget)

    @classmethod
    def _specialist_name(cls, call: Call) -> str:
        lead = call.lead
        specialist = None
        if lead is not None:
            specialist = lead.assigned_to
        if specialist is None:
            specialist = call.context_user
        if specialist is None:
            return ''
        return cls._text(getattr(specialist, 'full_name', ''))

    @classmethod
    def _linked_project_titles(cls, call: Call) -> str:
        try:
            titles = list(
                linked_projects_for_call(call)
                .order_by('title')
                .values_list('title', flat=True)
            )
        except ToolContextUnavailableError:
            return ''
        cleaned = [cls._text(title) for title in titles if cls._text(title)]
        return ', '.join(cleaned)

    @classmethod
    def _prior_call_summary(cls, call: Call) -> str:
        if call.lead_id is None:
            return ''
        prior = (
            Call.objects.filter(
                lead_id=call.lead_id,
                status=Call.Status.COMPLETED,
            )
            .exclude(pk=call.pk)
            .exclude(summary='')
            .order_by('-ended_at', '-created_at')
            .values_list('summary', flat=True)
            .first()
        )
        if not prior:
            return ''
        return ' '.join(str(prior).split())[:PRIOR_CALL_SUMMARY_MAX_LENGTH]

    @classmethod
    def _latest_proposal(cls, call: Call) -> GeneratedProposal | None:
        if call.lead_id is None:
            return None
        return (
            GeneratedProposal.objects.filter(
                lead_id=call.lead_id,
                status=GeneratedProposal.Status.COMPLETED,
            )
            .select_related('project')
            .order_by('-created_at')
            .first()
        )

    @classmethod
    def _proposal_project_title(
        cls,
        proposal: GeneratedProposal | None,
    ) -> str:
        if proposal is None:
            return ''
        return cls._text(proposal.project.title)

    @classmethod
    def _budget_fit_summary(cls, call: Call) -> str:
        lead = call.lead
        if lead is None or lead.estimated_budget is None:
            return ''
        try:
            project = (
                assigned_projects_for_call(call)
                .order_by('title')
                .first()
            )
        except ToolContextUnavailableError:
            return ''
        if project is None:
            return ''
        lowest_price = lowest_available_unit_price(project.pk)
        budget_fit = compute_budget_fit(lead.estimated_budget, lowest_price)
        if budget_fit == 'within':
            return (
                f'Assigned project "{project.title}" appears within the '
                f'recorded budget.'
            )
        if budget_fit == 'over':
            return (
                f'Assigned project "{project.title}" may exceed the '
                f'recorded budget.'
            )
        return f'Budget fit for "{project.title}" is unknown.'
