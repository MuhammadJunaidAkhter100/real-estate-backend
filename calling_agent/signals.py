import logging
from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver
from calling_agent.models import Call

logger = logging.getLogger(__name__)


@receiver(post_save, sender=Call)
def on_call_post_save(sender, instance: Call, created: bool, **kwargs):
    """Automatically trigger post-call analysis and task creation when a Call has transcript_data."""
    if not instance.transcript_data:
        return

    # Check if analysis is missing or summary/sentiments/intents are incomplete
    if not instance.summary or not instance.key_sentiments or not instance.detected_intents:
        def trigger_analysis():
            try:
                from calling_agent.tasks import process_call_transcript_task
                process_call_transcript_task.delay(instance.pk)
            except Exception:
                logger.exception("Failed to queue background transcript task for call %s; using direct analysis", instance.pk)
                try:
                    # The fallback bypasses the Celery task, so it has to bill the
                    # credit itself. The receipt is keyed on the call, so if the
                    # task was queued after all this is a no-op.
                    from calling_agent.services import charge_transcript_credits
                    from billing.exceptions import PlanLimitReached

                    try:
                        charge_transcript_credits(instance)
                    except PlanLimitReached:
                        logger.warning(
                            "AI credit limit reached; skipping transcript analysis for call %s",
                            instance.pk,
                        )
                        return
                    from calling_agent.transcript import analyze_and_process_call
                    analyze_and_process_call(instance)
                except Exception:
                    logger.exception("Failed direct fallback transcript analysis for call %s", instance.pk)

        transaction.on_commit(trigger_analysis)
