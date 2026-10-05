from __future__ import annotations

from unittest.mock import Mock

from django.test import SimpleTestCase, override_settings

from calling_agent.models import Call
from calling_agent.transfer import (
    TransferResolution,
    TransferResolutionService,
)


class TransferResolutionServiceTests(SimpleTestCase):
    @override_settings(ELEVENLABS_TRANSFER_ENABLED=True)
    def test_approved_resolver_contract_does_not_create_fallback(self) -> None:
        call = Mock(spec=Call)
        resolver = Mock()
        resolver.resolve.return_value = TransferResolution(
            available=True,
            destination='+442012345678',
            transfer_mode='blind',
            fallback_action='none',
            public_message='Connecting you to a specialist.',
        )
        task_service = Mock()
        service = TransferResolutionService(
            resolver=resolver,
            task_service=task_service,
        )

        resolution, task, fallback_created = service.resolve(
            call=call,
            reason='Specialist requested.',
        )

        self.assertTrue(resolution.available)
        self.assertEqual(resolution.destination, '+442012345678')
        self.assertIsNone(task)
        self.assertFalse(fallback_created)
        task_service.create_transfer_fallback.assert_not_called()
