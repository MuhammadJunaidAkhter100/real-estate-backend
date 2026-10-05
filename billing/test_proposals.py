"""Proposal quota and AI-credit enforcement at the shared worker seam.

Both the HTTP endpoint and the chatbot tool call `generate_proposal_pdf_task`,
so these tests drive that task directly and mock only the expensive parts.
"""

from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from billing import services
from new_proposal.tasks import (
    claim_proposal_generation,
    generate_proposal_pdf_task,
    proposal_charge_receipt,
)
from new_proposal.models import GeneratedProposal
from projects.models import Project, Unit
from users.models import Company, Lead, User


class ProposalBillingTestBase(TestCase):
    def setUp(self):
        self.company = Company.objects.create(
            name='Proposals Co', plan=Company.Plan.BASIC,
            operating_countries=['UAE'])
        self.user = User.objects.create_user(
            email='gen@proposals.test', first_name='G', last_name='P',
            password='Passw0rd!23', company=self.company,
            role=User.Role.AGENT, status=User.Status.ACTIVE)
        self.project = Project.objects.create(
            title='Marina Tower', description='A tower', location='Dubai',
            developer='Developer', starting_price=1000000, yield_percentage=7,
            project_type='residential', created_by=self.user)
        self.lead = Lead.objects.create(name='Lead One', created_by=self.user)
        self.unit = Unit.objects.create(
            project=self.project, label='A-1', list_price=1000000)
        self.proposal = GeneratedProposal.objects.create(
            project=self.project, lead=self.lead, unit=self.unit,
            generated_by=self.user, status=GeneratedProposal.Status.PENDING)

    def _usage(self, kind='ai_proposals'):
        return services.current_count(self.company.pk, kind)

    def _credits(self):
        return services.current_count(self.company.pk, 'ai_credits')


class ClaimProposalGenerationTests(ProposalBillingTestBase):
    def test_claims_one_proposal_and_ten_credits(self):
        receipt = proposal_charge_receipt('task-1', self.proposal.pk)
        self.assertTrue(claim_proposal_generation(self.proposal, receipt))

        self.assertEqual(self._usage('ai_proposals'), 1)
        self.assertEqual(self._credits(), 10)
        self.assertTrue(services.has_receipt(receipt))

    def test_same_receipt_claims_nothing_twice(self):
        receipt = proposal_charge_receipt('task-1', self.proposal.pk)
        self.assertTrue(claim_proposal_generation(self.proposal, receipt))
        self.assertFalse(claim_proposal_generation(self.proposal, receipt))

        self.assertEqual(self._usage('ai_proposals'), 1)
        self.assertEqual(self._credits(), 10)

    def test_different_task_ids_are_charged_separately(self):
        claim_proposal_generation(self.proposal, proposal_charge_receipt('task-1', 1))
        claim_proposal_generation(self.proposal, proposal_charge_receipt('task-2', 1))

        self.assertEqual(self._usage('ai_proposals'), 2)
        self.assertEqual(self._credits(), 20)

    def test_twenty_first_proposal_is_refused(self):
        from billing import services as svc

        svc.consume(self.company.pk, 'ai_proposals', 20)

        receipt = proposal_charge_receipt('task-x', self.proposal.pk)
        with self.assertRaises(svc.PlanLimitReached) as ctx:
            claim_proposal_generation(self.proposal, receipt)

        self.assertEqual(ctx.exception.kind, 'ai_proposals')
        self.assertEqual(ctx.exception.limit, 20)
        self.assertEqual(self._usage('ai_proposals'), 20)
        self.assertEqual(self._credits(), 0, 'a refused proposal costs nothing')

    def test_insufficient_credits_refuse_the_proposal(self):
        from billing import services as svc

        # Proposals left, but credits nearly exhausted.
        svc.consume(self.company.pk, 'ai_credits', 495)

        receipt = proposal_charge_receipt('task-y', self.proposal.pk)
        with self.assertRaises(svc.PlanLimitReached) as ctx:
            claim_proposal_generation(self.proposal, receipt)

        self.assertEqual(ctx.exception.kind, 'ai_credits')
        self.assertEqual(self._usage('ai_proposals'), 0, 'the rolled-back claim leaves nothing')
        self.assertEqual(self._credits(), 495)
        self.assertFalse(
            services.has_receipt(receipt), 'the receipt is released on rollback')

    def test_professional_and_manual_are_unlimited(self):
        for expected, plan in ((25, Company.Plan.PROFESSIONAL),
                               (50, Company.Plan.MANUAL)):
            self.company.plan = plan
            self.company.save(update_fields=['plan'])
            self.proposal.generated_by.refresh_from_db()
            for index in range(25):
                claim_proposal_generation(
                    self.proposal, proposal_charge_receipt(f'{plan}-{index}', index))
            # Usage is still recorded for analytics, but never blocks.
            self.assertEqual(self._usage('ai_proposals'), expected, plan)

    def test_unattributable_proposals_are_not_metered(self):
        self.proposal.generated_by = None
        receipt = proposal_charge_receipt('task-none', self.proposal.pk)

        self.assertFalse(claim_proposal_generation(self.proposal, receipt))
        self.assertEqual(self._usage('ai_proposals'), 0)
        self.assertEqual(self._credits(), 0)


class GenerateProposalTaskBillingTests(ProposalBillingTestBase):
    """The task must charge exactly once and refund on failure."""

    def _run(self, task_id='task-run'):
        result = generate_proposal_pdf_task.apply(
            args=[self.proposal.pk], task_id=task_id).get()
        self.proposal.refresh_from_db()
        return result

    def test_success_charges_once(self):
        with patch('new_proposal.tasks._cleanup_output_dir'), \
             patch('new_proposal.extraction.project_assets'
                   '.ensure_project_proposal_assets', return_value={'facts': {'a': 1}}), \
             patch('new_proposal.extraction.proposal_pdf.generate_proposal_pdf',
                   return_value=b'%PDF-1.4 fake'):
            result = self._run()

        self.assertTrue(result['success'], result)
        self.assertEqual(self.proposal.status, GeneratedProposal.Status.COMPLETED)
        self.assertEqual(self._usage('ai_proposals'), 1)
        self.assertEqual(self._credits(), 10)

    def test_retry_of_the_same_task_does_not_charge_twice(self):
        with patch('new_proposal.tasks._cleanup_output_dir'), \
             patch('new_proposal.extraction.project_assets'
                   '.ensure_project_proposal_assets', return_value={'facts': {}}), \
             patch('new_proposal.extraction.proposal_pdf.generate_proposal_pdf',
                   return_value=b'%PDF-1.4 fake'):
            self._run(task_id='retry-me')
            self._run(task_id='retry-me')

        self.assertEqual(self._usage('ai_proposals'), 1)
        self.assertEqual(self._credits(), 10)

    def test_failure_refunds_the_charge(self):
        from new_proposal.extraction.proposal_pdf import ProposalPdfError

        with patch('new_proposal.tasks._cleanup_output_dir'), \
             patch('new_proposal.extraction.project_assets'
                   '.ensure_project_proposal_assets', return_value={'facts': {}}), \
             patch('new_proposal.extraction.proposal_pdf.generate_proposal_pdf',
                   side_effect=ProposalPdfError('boom')):
            result = self._run()

        self.assertFalse(result['success'])
        self.assertEqual(self.proposal.status, GeneratedProposal.Status.FAILED)
        self.assertEqual(self._usage('ai_proposals'), 0)
        self.assertEqual(self._credits(), 0)

    def test_plan_limit_marks_the_proposal_failed_without_charging(self):
        services.consume(self.company.pk, 'ai_proposals', 20)

        with patch('new_proposal.tasks._cleanup_output_dir'), \
             patch('new_proposal.extraction.project_assets'
                   '.ensure_project_proposal_assets') as assets:
            result = self._run()

        self.assertFalse(result['success'])
        self.assertEqual(result['code'], 'PLAN_LIMIT_REACHED')
        self.assertEqual(result['kind'], 'ai_proposals')
        self.assertEqual(result['limit'], 20)
        self.assertEqual(self.proposal.status, GeneratedProposal.Status.FAILED)
        assets.assert_not_called()

    def test_refund_lets_a_retry_succeed(self):
        from new_proposal.extraction.proposal_pdf import ProposalPdfError

        with patch('new_proposal.tasks._cleanup_output_dir'), \
             patch('new_proposal.extraction.project_assets'
                   '.ensure_project_proposal_assets', return_value={'facts': {}}), \
             patch('new_proposal.extraction.proposal_pdf.generate_proposal_pdf',
                   side_effect=ProposalPdfError('boom')):
            self._run(task_id='attempt-1')

        self.assertEqual(self._usage('ai_proposals'), 0)

        with patch('new_proposal.tasks._cleanup_output_dir'), \
             patch('new_proposal.extraction.project_assets'
                   '.ensure_project_proposal_assets', return_value={'facts': {}}), \
             patch('new_proposal.extraction.proposal_pdf.generate_proposal_pdf',
                   return_value=b'%PDF-1.4 fake'):
            result = self._run(task_id='attempt-2')

        self.assertTrue(result['success'], result)
        self.assertEqual(self._usage('ai_proposals'), 1)
        self.assertEqual(self._credits(), 10)


class ProposalQuotaResetsMonthlyTests(ProposalBillingTestBase):
    def test_counters_are_scoped_to_the_utc_month(self):
        from billing import plan_limits

        this_month = plan_limits.current_period_start()
        next_month = plan_limits.current_period_start(
            now=timezone.now() + timedelta(days=32))
        self.assertGreater(next_month, this_month)

        services.consume(self.company.pk, 'ai_proposals', 20)
        self.assertEqual(self._usage('ai_proposals'), 20)

        # A brand-new period starts empty, so a company blocked this month can
        # work again next month.
        services.consume(self.company.pk, 'ai_proposals', 20, now=timezone.now() + timedelta(days=32))
        self.assertEqual(
            services.current_count(
                self.company.pk, 'ai_proposals', now=timezone.now() + timedelta(days=32)),
            20)
