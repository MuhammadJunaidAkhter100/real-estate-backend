from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from django.conf import settings

from calling_agent.action_services import TaskActionService
from calling_agent.models import Call
from users.models import Task


@dataclass(frozen=True)
class TransferResolution:
    available: bool
    destination: str | None
    transfer_mode: str | None
    fallback_action: str
    public_message: str


class TransferDestinationResolver(Protocol):
    def resolve(self, call: Call, reason: str) -> TransferResolution:
        """Resolve an approved transfer destination without side effects."""


class DisabledTransferDestinationResolver:
    def resolve(self, call: Call, reason: str) -> TransferResolution:
        return TransferResolution(
            available=False,
            destination=None,
            transfer_mode=None,
            fallback_action='create_follow_up_task',
            public_message=(
                'A specialist is not available for transfer. '
                'A follow-up has been requested.'
            ),
        )


class TransferResolutionService:
    def __init__(
        self,
        resolver: TransferDestinationResolver | None = None,
        task_service: TaskActionService | None = None,
    ) -> None:
        self.resolver = resolver or DisabledTransferDestinationResolver()
        self.task_service = task_service or TaskActionService()

    def resolve(
        self,
        *,
        call: Call,
        reason: str,
    ) -> tuple[TransferResolution, Task | None, bool]:
        if settings.ELEVENLABS_TRANSFER_ENABLED:
            resolution = self.resolver.resolve(call, reason)
        else:
            resolution = DisabledTransferDestinationResolver().resolve(
                call,
                reason,
            )

        if resolution.available:
            if not resolution.destination or not resolution.transfer_mode:
                raise ValueError(
                    'An available transfer requires a destination and mode.'
                )
            return resolution, None, False

        _action, task, created = self.task_service.create_transfer_fallback(
            call=call,
            reason=reason,
        )
        return resolution, task, created
