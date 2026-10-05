from django.conf import settings
from django.db import models



class WhatsAppAccount(models.Model):
    """A WhatsApp account session linked to a Django user."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="whatsapp_account",
    )
    provider = models.CharField(max_length=32, default="waha")
    session_name = models.CharField(max_length=128, blank=True, default="")
    session_status = models.CharField(max_length=32, blank=True, default="not_created")
    name = models.CharField(max_length=255, blank=True, default="")
    number = models.CharField(max_length=64, blank=True, default="")
    profile_picture = models.URLField(max_length=1024, blank=True, default="")
    media_api_key = models.CharField(max_length=255, blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"WA[{self.name or self.session_name or self.user_id}] - {self.user_id}"


class WhatsAppChat(models.Model):
    account = models.ForeignKey(
        WhatsAppAccount, on_delete=models.CASCADE, related_name="chats",
    )
    chat_id = models.CharField(max_length=128)
    name = models.CharField(max_length=255, blank=True, default="")
    is_group = models.BooleanField(default=False)
    profile_picture = models.URLField(max_length=1024, blank=True, default="")

    last_message_at = models.DateTimeField(null=True, blank=True)
    unread_count = models.IntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("account", "chat_id")
        ordering = ["-last_message_at", "-updated_at"]

    def __str__(self):
        return f"{self.name or self.chat_id}"


class WhatsAppMessage(models.Model):
    class Direction(models.TextChoices):
        INCOMING = "in", "Incoming"
        OUTGOING = "out", "Outgoing"

    class AckStatus(models.TextChoices):
        ERROR = "ERROR", "Error"
        PENDING = "PENDING", "Pending"
        SENT = "SERVER", "Sent"
        DELIVERED = "DEVICE", "Delivered"
        READ = "READ", "Read"
        PLAYED = "PLAYED", "Played"

    chat = models.ForeignKey(
        WhatsAppChat, on_delete=models.CASCADE, related_name="messages",
    )
    message_id = models.CharField(max_length=128)
    direction = models.CharField(max_length=8, choices=Direction.choices)
    type = models.CharField(max_length=32, blank=True, default="")
    text = models.TextField(blank=True, default="")
    sender_id = models.CharField(max_length=128, blank=True, default="")
    sender_name = models.CharField(max_length=255, blank=True, default="")
    timestamp = models.DateTimeField(null=True, blank=True)

    # Message delivery and read status (WAHA ack)
    ack = models.CharField(max_length=16, choices=AckStatus.choices, default=AckStatus.PENDING, db_index=True)
    ack_code = models.IntegerField(default=1)  # 1=Pending, 2=Server/Sent, 3=Device/Delivered, 4=Read, 5=Played

    # Media attachments (image / document / video / audio). Empty for text-only
    # messages. `media_url` is the downloadable file URL from Green API.
    media_url = models.URLField(max_length=1024, blank=True, default="")
    file_name = models.CharField(max_length=512, blank=True, default="")
    mime_type = models.CharField(max_length=128, blank=True, default="")
    caption = models.TextField(blank=True, default="")
    reaction = models.CharField(max_length=64, blank=True, null=True, default="")

    raw = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("chat", "message_id")
        ordering = ["-timestamp"]

    def __str__(self):
        return f"{self.direction} {self.message_id}"



class WhatsAppDeletedMessage(models.Model):
    """Track message IDs that have been explicitly deleted by users so that

    future sync operations or incoming webhooks do not restore them.
    """

    account = models.ForeignKey(
        WhatsAppAccount,
        on_delete=models.CASCADE,
        related_name="deleted_messages",
    )
    chat_id = models.CharField(max_length=128, db_index=True)
    message_id = models.CharField(max_length=128, db_index=True)

    deleted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("account", "chat_id", "message_id")
        ordering = ["-deleted_at"]

    def __str__(self):
        return f"Deleted {self.chat_id} - {self.message_id}"

