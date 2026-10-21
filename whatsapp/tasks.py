import logging
from typing import Any

from celery import shared_task
from django.contrib.auth import get_user_model
from django.conf import settings
from django.db import transaction

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


@shared_task(name="whatsapp.auto_reply_from_kb")
def auto_reply_from_kb_task(account_id: int, chat_id: int, text: str) -> dict[str, Any]:
    """Generate and send a KB-grounded reply outside the webhook request."""
    from whatsapp.models import WhatsAppAccount, WhatsAppChat

    logger.info(
        "whatsapp.auto_reply: task started account_id=%s chat_id=%s message_length=%s",
        account_id,
        chat_id,
        len(text or ""),
    )
    account = WhatsAppAccount.objects.select_related("user").filter(pk=account_id).first()
    if not account:
        logger.warning("whatsapp.auto_reply: account not found account_id=%s", account_id)
        return {"status": "account_not_found"}

    chat = WhatsAppChat.objects.filter(pk=chat_id, account=account).first()
    if not chat:
        logger.warning(
            "whatsapp.auto_reply: chat not found account_id=%s chat_id=%s",
            account_id,
            chat_id,
        )
        return {"status": "chat_not_found"}

    try:
        result = WhatsAppService()._maybe_auto_reply_from_kb(account, chat, text)
        if result is None:
            logger.info(
                "whatsapp.auto_reply: skipped account_id=%s chat_id=%s",
                account_id,
                chat_id,
            )
            return {"status": "skipped"}

        logger.info(
            "whatsapp.auto_reply: finished account_id=%s chat_id=%s send_status=%s",
            account_id,
            chat_id,
            result.get("status", "unknown"),
        )
        return result
    except Exception as exc:  # noqa: BLE001
        logger.exception("whatsapp.auto_reply: failed for account %s, chat %s", account_id, chat_id)
        return {"status": "error", "message": str(exc)}


@shared_task(name="whatsapp.send_company_admin_welcome")
def send_company_admin_welcome_task(
    company_id: int,
    admin_id: int,
    sender_user_id: int | None = None,
) -> dict[str, Any]:
    """Send a welcome message to a company phone using a superadmin session."""
    from users.models import Company, User
    from whatsapp.models import WhatsAppAccount

    company = Company.objects.filter(pk=company_id).first()
    admin = User.objects.filter(pk=admin_id, company_id=company_id).first()
    if not company or not admin:
        return {"status": "company_or_admin_not_found"}

    if not company.phone.strip():
        return {"status": "phone_missing"}

    sender_accounts = WhatsAppAccount.objects.filter(
        user__role=User.Role.SUPERADMIN,
        session_status__iexact="WORKING",
    ).select_related("user")
    if sender_user_id:
        sender_accounts = sender_accounts.filter(user_id=sender_user_id)
    sender_account = sender_accounts.order_by("created_at").first()
    if not sender_account:
        return {"status": "superadmin_whatsapp_not_connected"}

    service = WhatsAppService()
    number_result = service.check_number_exists(sender_account.user, company.phone)
    if number_result.get("status") != "success" or number_result.get("numberExists") is not True:
        return {"status": "number_not_on_whatsapp", "check": number_result}

    chat_id = number_result.get("chatId")
    if not chat_id:
        return {"status": "whatsapp_chat_id_missing"}

    first_name = admin.first_name.strip() or "there"
    welcome_lines = [
        f"Hello {first_name},",
        "",
        f"Welcome to Nexusproperty. The company admin account for {company.name} is ready.",
    ]
    if company.website.strip():
        welcome_lines.extend([
            "",
            f"We've started processing the website you shared: {company.website}",
            "This will help us prepare your workspace with your company's information.",
        ])
    welcome_lines.extend([
        "",
        f"Log in to your account: {settings.FRONTEND_LOGIN_URL}",
        "",
        "We're glad to have you with us.",
        "Nexusproperty Team",
    ])
    welcome_text = "\n".join(welcome_lines)
    result = service.send_text_message(
        user=sender_account.user,
        payload={"chat_id": chat_id, "text": welcome_text},
    )
    return {"status": "sent", "result": result}


@shared_task(name="whatsapp.send_scrape_completion_notification")
def send_scrape_completion_notification_task(
    company_id: int,
    website: str,
    project_count: int = 0,
    document_url: str | None = None,
) -> dict[str, Any]:
    """Notify a company via WhatsApp after a successful website scrape.

    Uses a connected superadmin WhatsApp session, verifies the company phone is
    registered on WhatsApp, and then sends the completion note with the scrubbed
    website URL and the optional knowledge-base document URL.
    """
    from users.models import Company, User
    from whatsapp.models import WhatsAppAccount

    company = Company.objects.filter(pk=company_id).first()
    if not company:
        return {"status": "company_not_found"}
    if not company.phone.strip():
        return {"status": "phone_missing"}

    sender_accounts = WhatsAppAccount.objects.filter(
        user__role=User.Role.SUPERADMIN,
        session_status__iexact="WORKING",
    ).select_related("user")
    sender_account = sender_accounts.order_by("created_at").first()
    if not sender_account:
        return {"status": "superadmin_whatsapp_not_connected"}

    service = WhatsAppService()
    number_result = service.check_number_exists(sender_account.user, company.phone)
    if number_result.get("status") != "success" or number_result.get("numberExists") is not True:
        return {"status": "number_not_on_whatsapp", "check": number_result}

    chat_id = number_result.get("chatId")
    if not chat_id:
        return {"status": "whatsapp_chat_id_missing"}

    message_lines = [
        "Website scraping is complete.",
        f"Company: {company.name}",
        f"Website: {website or '-'}",
        f"Projects saved: {project_count}",
    ]
    if document_url:
        message_lines.extend(["", f"Knowledge base document: {document_url}"])

    result = service.send_text_message(
        user=sender_account.user,
        payload={"chat_id": chat_id, "text": "\n".join(message_lines)},
    )
    return {"status": "sent", "result": result}


def queue_company_admin_welcome(
    company_id: int,
    admin_id: int,
    sender_user_id: int | None = None,
) -> None:
    """Queue the welcome only after company creation commits successfully."""
    def _queue() -> None:
        try:
            send_company_admin_welcome_task.delay(company_id, admin_id, sender_user_id)
        except Exception:  # noqa: BLE001
            logger.exception("Could not queue WhatsApp welcome for company %s", company_id)

    transaction.on_commit(_queue)


def queue_scrape_completion_notification(
    company_id: int,
    website: str,
    project_count: int = 0,
    document_url: str | None = None,
) -> None:
    """Queue the website scrape completion message after the scrape transaction commits."""
    def _queue() -> None:
        try:
            from whatsapp.tasks import send_scrape_completion_notification_task
            send_scrape_completion_notification_task.delay(
                company_id,
                website,
                project_count,
                document_url,
            )
        except Exception:  # noqa: BLE001
            logger.exception("Could not queue WhatsApp scrape-completion notification for company %s", company_id)

    transaction.on_commit(_queue)

