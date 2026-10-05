from rest_framework import serializers

from rest_framework import serializers


class WhatsAppSendMessageSerializer(serializers.Serializer):
    TYPE_CHOICES = (
        ("text", "Text"),
        ("image", "Image"),
        ("document", "Document"),
        ("video", "Video"),
        ("audio", "Audio"),
    )

    chat_id = serializers.CharField(
        required=True,
        help_text="WhatsApp chat ID"
    )

    type = serializers.ChoiceField(
        choices=TYPE_CHOICES,
        required=True,
    )

    text = serializers.CharField(
        required=False,
        allow_blank=True,
    )

    caption = serializers.CharField(
        required=False,
        allow_blank=True,
    )

    file = serializers.FileField(
        required=False,
        allow_null=True,
    )

    def validate(self, attrs):
        message_type = attrs.get("type")

        if message_type == "text":
            if not attrs.get("text"):
                raise serializers.ValidationError(
                    {"text": "This field is required for text messages."}
                )
        else:
            if not attrs.get("file"):
                raise serializers.ValidationError(
                    {"file": "This field is required for media messages."}
                )

        return attrs

class WhatsAppMessageSerializer(serializers.Serializer):
    """Serializer for WhatsApp message objects."""

    id = serializers.CharField()
    message_id = serializers.CharField()
    direction = serializers.CharField()
    type = serializers.CharField()
    text = serializers.CharField()
    sender_id = serializers.CharField()
    sender_name = serializers.CharField()
    timestamp = serializers.CharField()
    ack = serializers.CharField(required=False, default="PENDING")
    ack_code = serializers.IntegerField(required=False, default=1)
    media_url = serializers.CharField()
    file_name = serializers.CharField()
    mime_type = serializers.CharField()
    caption = serializers.CharField()
    reaction = serializers.CharField(required=False, allow_blank=True, default="")
    created_at = serializers.CharField()


class WhatsAppChatSerializer(serializers.Serializer):
    """Serializer for WhatsApp chat objects."""

    id = serializers.CharField()
    chat_id = serializers.CharField()
    name = serializers.CharField()
    profile_picture_url = serializers.CharField(required=False, allow_blank=True, default="")
    is_group = serializers.BooleanField()
    unread_count = serializers.IntegerField(default=0)
    is_read = serializers.BooleanField(default=True)
    last_message_at = serializers.CharField(required=False, allow_null=True)
    last_message = WhatsAppMessageSerializer(required=False, allow_null=True)
    created_at = serializers.CharField()
    updated_at = serializers.CharField()


class WhatsAppWebhookSerializer(serializers.Serializer):
    """Serializer for WhatsApp webhook events from the provider."""

    event = serializers.CharField(
        max_length=32,
        required=False,
        help_text="Event type (e.g., 'message')"
    )
    session = serializers.CharField(
        max_length=128,
        required=False,
        help_text="WhatsApp session name"
    )
    id = serializers.CharField(
        max_length=128,
        required=False,
        help_text="Message ID"
    )
    timestamp = serializers.IntegerField(
        required=False,
        help_text="Message timestamp (Unix epoch)"
    )
    body = serializers.CharField(
        required=False,
        help_text="Message text content"
    )
    text = serializers.CharField(
        required=False,
        help_text="Message text content (alternative field)"
    )
    type = serializers.CharField(
        max_length=32,
        required=False,
        default="text",
        help_text="Message type"
    )
    to = serializers.CharField(
        max_length=128,
        required=False,
        help_text="Recipient ID"
    )
    sender = serializers.CharField(
        max_length=128,
        required=False,
        help_text="Sender ID (alternative field)"
    )
    senderName = serializers.CharField(
        max_length=255,
        required=False,
        help_text="Sender name"
    )

    def to_representation(self, instance):
        """Allow 'from' field in the data."""
        data = super().to_representation(instance)
        if 'from' in instance:
            data['from'] = instance['from']
        return data


class WhatsAppAccountSerializer(serializers.Serializer):
    """Serializer for WhatsApp account details."""

    id = serializers.IntegerField(read_only=True)
    user_id = serializers.IntegerField(read_only=True)
    provider = serializers.CharField(read_only=True)
    session_name = serializers.CharField(read_only=True)
    session_status = serializers.CharField(read_only=True)
    name = serializers.CharField(read_only=True)
    number = serializers.CharField(read_only=True)
    profile_picture = serializers.CharField(read_only=True)
    created_at = serializers.DateTimeField(read_only=True)
    updated_at = serializers.DateTimeField(read_only=True)


class WhatsAppUpdateProfileNameSerializer(serializers.Serializer):
    """Serializer for updating profile display name."""

    name = serializers.CharField(
        required=True,
        max_length=255,
        help_text="New profile display name"
    )


class WhatsAppUpdateProfileStatusSerializer(serializers.Serializer):
    """Serializer for updating profile status/about text."""

    status = serializers.CharField(
        required=True,
        max_length=512,
        help_text="New profile status (About text)"
    )


class WhatsAppUpdateProfilePictureSerializer(serializers.Serializer):
    """Serializer for updating profile picture."""

    file = serializers.FileField(
        required=False,
        allow_null=True,
        help_text="Image file for profile picture"
    )
   
    def validate(self, attrs):
        if not attrs.get("file"):
            raise serializers.ValidationError(
                "file is required."
            )
        return attrs


class WhatsAppSaveContactSerializer(serializers.Serializer):
    """Serializer for saving/updating a WhatsApp contact."""

    phone = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text="Phone number or WhatsApp contact ID (e.g. 923001234567, +923001234567, or 923001234567@c.us)",
    )
    firstName = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text="First name of the contact",
    )
    lastName = serializers.CharField(
        required=False,
        allow_blank=True,
        default="",
        help_text="Last name of the contact",
    )

    # Optional alias fields for backwards/snake_case compatibility
    first_name = serializers.CharField(required=False, allow_blank=True)
    last_name = serializers.CharField(required=False, allow_blank=True, default="")
    number = serializers.CharField(required=False, allow_blank=True)

    def validate(self, attrs):
        phone = attrs.get("phone") or attrs.get("number")
        if not phone:
            raise serializers.ValidationError({"phone": "Phone number ('phone' or 'number') is required."})

        first_name = attrs.get("firstName") or attrs.get("first_name")
        if not first_name:
            raise serializers.ValidationError({"firstName": "First name ('firstName' or 'first_name') is required."})

        last_name = attrs.get("lastName") or attrs.get("last_name") or ""

        return {
            "phone": phone,
            "first_name": first_name,
            "last_name": last_name,
        }


class WhatsAppReactionSerializer(serializers.Serializer):
    """Serializer for sending a reaction to a WhatsApp message."""

    messageId = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text="The ID of the message to react to (e.g. false_11111111111@c.us_AAAAAAAAAAAAAAAAAAAA)",
    )

    reaction = serializers.CharField(
        required=True,
        allow_blank=True,
        help_text="The emoji reaction to send (e.g. 👍, ❤️, 😂) or empty string to remove reaction",
    )
   
    def validate(self, attrs):
        message_id = attrs.get("messageId") 
        if not message_id:
            raise serializers.ValidationError(
                {"messageId": "Message ID ('messageId' or 'message_id') is required."}
            )
        attrs["messageId"] = message_id
        return attrs



