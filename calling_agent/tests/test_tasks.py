from __future__ import annotations

from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from calling_agent.models import Call
from calling_agent.tasks import (
    claim_due_calls_task,
    initiate_scheduled_call_task,
    reconcile_scheduled_calls_task,
)


class ScheduledCallTaskTests(SimpleTestCase):
    @patch('calling_agent.tasks._dispatch_call_tasks', return_value=2)
    @patch('calling_agent.tasks.ScheduledCallService')
    def test_claim_task_dispatches_each_claimed_call(
        self,
        service_class: Mock,
        dispatch: Mock,
    ) -> None:
        service_class.return_value.claim_due_calls.return_value = [11, 12]

        result = claim_due_calls_task.run()

        dispatch.assert_called_once_with([11, 12])
        self.assertEqual(result['claimed_count'], 2)
        self.assertEqual(result['dispatched_count'], 2)

    @patch('calling_agent.tasks.ScheduledCallService')
    def test_initiation_task_reports_cancelled_claim(
        self,
        service_class: Mock,
    ) -> None:
        call = Mock(status=Call.Status.CANCELLED)
        service_class.return_value.initiate_claimed_call.return_value = (
            call,
            False,
        )

        result = initiate_scheduled_call_task.run(11)

        self.assertEqual(result['status'], 'ignored')
        self.assertEqual(result['call_status'], Call.Status.CANCELLED)

    @patch('calling_agent.tasks._dispatch_call_tasks', return_value=1)
    @patch('calling_agent.tasks.ScheduledCallService')
    def test_reconciliation_marks_unknown_before_reclaiming(
        self,
        service_class: Mock,
        dispatch: Mock,
    ) -> None:
        service = service_class.return_value
        service.mark_stale_initiations_unknown.return_value = [10]
        service.reclaim_expired_claims.return_value = [12]

        result = reconcile_scheduled_calls_task.run()

        service.mark_stale_initiations_unknown.assert_called_once_with()
        service.reclaim_expired_claims.assert_called_once_with()
        dispatch.assert_called_once_with([12])
        self.assertEqual(result['marked_unknown_count'], 1)
        self.assertEqual(result['reclaimed_count'], 1)
