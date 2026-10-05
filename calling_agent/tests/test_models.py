from __future__ import annotations

from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from calling_agent.models import Call
from calling_agent.tests.factories import create_company, create_lead, create_user


class CallModelTests(TestCase):
    def setUp(self) -> None:
        self.company = create_company()
        self.user = create_user(company=self.company)
        self.lead = create_lead(user=self.user)

    def test_duration_formats_seconds(self) -> None:
        call = Call.objects.create(
            company=self.company,
            lead=self.lead,
            context_user=self.user,
            lead_name=self.lead.name,
            phone_number=self.lead.phone_no,
            duration_seconds=148,
        )

        self.assertEqual(call.duration, '02:28')

    def test_provider_conversation_id_is_unique_when_present(self) -> None:
        Call.objects.create(
            company=self.company,
            provider_conversation_id='conversation-1',
        )

        with self.assertRaises(IntegrityError):
            Call.objects.create(
                company=self.company,
                provider_conversation_id='conversation-1',
            )

    def test_provider_conversation_id_allows_multiple_null_values(self) -> None:
        Call.objects.create(company=self.company)
        Call.objects.create(company=self.company)

        self.assertEqual(Call.objects.count(), 2)

    def test_scheduled_attempt_is_unique_per_lead_and_time(self) -> None:
        scheduled_for = timezone.now()
        Call.objects.create(
            company=self.company,
            lead=self.lead,
            scheduled_for=scheduled_for,
            attempt_number=1,
        )

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Call.objects.create(
                    company=self.company,
                    lead=self.lead,
                    scheduled_for=scheduled_for,
                    attempt_number=1,
                )
