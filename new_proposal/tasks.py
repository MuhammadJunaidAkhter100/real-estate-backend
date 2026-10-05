from __future__ import annotations

import gc
import logging
import os
import shutil
import stat
import time

from celery import shared_task
from django.core.files.base import ContentFile
from django.db import transaction

from billing import services
from billing.exceptions import PlanLimitReached

logger = logging.getLogger(__name__)

_OUTPUT_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "extraction", "output"
)


def _force_remove(func, path, exc_info):
    """rmtree onerror handler: drop the read-only bit and retry the call."""
    try:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
        func(path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("force_remove failed for %s: %s", path, exc)


def _cleanup_output_dir(project_id) -> None:
    """Remove the per-project working folder after a proposal has been saved.

    Windows can briefly hold file handles open (Chromium, Pillow) so we retry
    a few times and force-clear the read-only bit if needed.
    """
    project_dir = os.path.join(_OUTPUT_DIR, str(project_id))
    if not os.path.isdir(project_dir):
        logger.info("Cleanup: no output dir for project %s", project_id)
        return

    gc.collect()
    last_exc: Exception | None = None
    for attempt in range(1, 4):
        try:
            shutil.rmtree(project_dir, onerror=_force_remove)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            time.sleep(0.5 * attempt)
            continue

        if not os.path.exists(project_dir):
            logger.info("Cleaned output dir %s (attempt %d)", project_dir, attempt)
            return

        time.sleep(0.5 * attempt)

    logger.warning(
        "Could not clean output dir %s after retries: %s", project_dir, last_exc,
    )


@shared_task(bind=True, name="new_proposal.generate_proposal_pdf")
def generate_proposal_pdf_task(self, proposal_id: int, force_refresh: bool = False):
    """Render and persist the proposal PDF for a `GeneratedProposal` row.

    When ``force_refresh`` is True the project's proposal assets and AI facts
    are re-extracted even if the cached fingerprint matches.
    """
    from new_proposal.extraction.project_assets import (
        ensure_project_proposal_assets,
    )
    from new_proposal.extraction.proposal_pdf import (
        ProposalPdfError,
        generate_proposal_pdf,
    )
    from new_proposal.models import GeneratedProposal

    try:
        proposal = GeneratedProposal.objects.select_related(
            "project", "lead", "unit", "generated_by"
        ).get(pk=proposal_id)
    except GeneratedProposal.DoesNotExist:
        logger.warning("generate_proposal_pdf_task: proposal %s not found", proposal_id)
        return {"success": False, "error": "Proposal not found"}

    proposal.status = GeneratedProposal.Status.PROCESSING
    proposal.error = ""
    proposal.task_id = self.request.id or ""
    proposal.save(update_fields=["status", "error", "task_id", "updated_at"])

    # Billing: this is the single seam both the HTTP endpoint and the chatbot
    # tool funnel through, so it is where the proposal quota and the AI credits
    # are claimed. The Celery task id keys the receipt, so a retry of *this*
    # task re-runs the work without being charged twice.
    receipt = proposal_charge_receipt(self.request.id, proposal_id)
    charged_here = False
    try:
        charged_here = claim_proposal_generation(proposal, receipt)
    except PlanLimitReached as exc:
        proposal.status = GeneratedProposal.Status.FAILED
        proposal.error = str(exc.detail)
        proposal.save(update_fields=["status", "error", "updated_at"])
        logger.warning(
            "Proposal %s blocked by plan limit: %s", proposal_id, exc.detail)
        return {
            "success": False,
            "error": str(exc.detail),
            "code": exc.default_code,
            "kind": exc.kind,
            "limit": exc.limit,
            "current": exc.current,
        }

    try:
        assets = ensure_project_proposal_assets(proposal.project, force_refresh=force_refresh)

        pdf_bytes = generate_proposal_pdf(
            proposal.project, proposal.unit, proposal.lead,
            assets, current_user=proposal.generated_by,
        )

        filename = (
            f"proposal_project_{proposal.project_id}"
            f"_unit_{proposal.unit_id}_lead_{proposal.lead_id}.pdf"
        )
        proposal.file.save(filename, ContentFile(pdf_bytes), save=False)
        try:
            proposal.hosted_url = proposal.file.url
        except Exception:  # noqa: BLE001
            proposal.hosted_url = ""

        proposal.ai_facts = assets.get("facts") or {}
        proposal.status = GeneratedProposal.Status.COMPLETED
        proposal.save(update_fields=[
            "file", "hosted_url", "ai_facts", "status", "updated_at",
        ])

        return {
            "success": True,
            "proposal_id": proposal.pk,
            "hosted_url": proposal.hosted_url,
        }

    except ProposalPdfError as exc:
        proposal.status = GeneratedProposal.Status.FAILED
        proposal.error = str(exc)
        proposal.save(update_fields=["status", "error", "updated_at"])
        logger.exception("Proposal PDF generation failed for proposal %s", proposal_id)
        return _failed(proposal, exc, charged_here, receipt)
    except Exception as exc:  # noqa: BLE001
        proposal.status = GeneratedProposal.Status.FAILED
        proposal.error = str(exc)
        proposal.save(update_fields=["status", "error", "updated_at"])
        logger.exception("Unexpected failure for proposal %s", proposal_id)
        return _failed(proposal, exc, charged_here, receipt)
    finally:
        _cleanup_output_dir(proposal.project_id)


def proposal_charge_receipt(task_id, proposal_id):
    """Stable receipt key for one run of one proposal.

    Keyed on the Celery task id so a retry reuses the receipt, while a fresh
    request to generate the same proposal again is charged again.
    """
    return f"proposal-pdf:{task_id or proposal_id}"


def _failed(proposal, exc, charged_here, receipt):
    """Refund the work that did not happen, then report the failure."""
    from new_proposal.models import GeneratedProposal

    if charged_here:
        company = getattr(proposal.generated_by, "company", None)
        if company is not None:
            services.refund_proposal_generation(company, receipt)
    return {
        "success": False,
        "proposal_id": proposal.pk,
        "error": str(exc),
        "status": GeneratedProposal.Status.FAILED,
    }


def claim_proposal_generation(proposal, receipt):
    """Claim one proposal from the monthly quota and its 10 AI credits.

    Returns True when *this* attempt performed the charge (so a later failure
    should refund it), and False when a previous attempt already paid.
    """
    company = getattr(proposal.generated_by, "company", None)
    if company is None:
        # Unattributable (no company context): never metered.
        return False
    if services.has_receipt(receipt):
        # This exact work was charged already - a retry, not new usage.
        return False

    with transaction.atomic():
        service = services.PlanLimitsService.for_company(company)
        service.assert_can_add("ai_proposals")
        service.consume("ai_proposals", 1)
        service.consume_credits("ai_proposal", receipt_id=receipt)
    return True


def check_proposal_quota(user):
    """Early cap check so callers get an immediate 403 instead of a queued failure."""
    company = getattr(user, "company", None)
    if company is None:
        return
    services.PlanLimitsService.for_company(company).assert_can_add("ai_proposals")
