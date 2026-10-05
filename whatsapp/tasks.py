import logging
from typing import Any

from celery import shared_task
from django.contrib.auth import get_user_model

from whatsapp.services import WhatsAppService
from django.utils import timezone


# Import model lazily inside the task to avoid app registry/circular import issues

logger = logging.getLogger(__name__)


@shared_task(bind=True, name="whatsapp.sync_chats_and_messages")
def sync_chats_and_messages_task(self, user_id: int) -> dict[str, Any]:
    """Celery task wrapper to sync WhatsApp chats/messages for a user.

    The task returns the result produced by `WhatsAppService.sync_chats_and_messages`.
    """
    User = get_user_model()
    try:
        user = User.objects.get(pk=user_id)
    except User.DoesNotExist:
        logger.warning("whatsapp.sync: user %s does not exist", user_id)
        return {"status": "user_not_found", "chats_synced": 0, "messages_synced": 0}

    service = WhatsAppService()

    try:
        result = service.sync_chats_and_messages(user=user)
        return result
    except Exception as exc:  # noqa: BLE001
        logger.exception("whatsapp.sync: failed for user %s", user_id)
        return {"status": "error", "message": str(exc), "chats_synced": 0, "messages_synced": 0}

