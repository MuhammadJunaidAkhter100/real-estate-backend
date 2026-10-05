from django.contrib import admin

from whatsapp.models import WhatsAppAccount, WhatsAppChat, WhatsAppMessage, WhatsAppDeletedMessage


@admin.register(WhatsAppAccount)
class WhatsAppAccountAdmin(admin.ModelAdmin):
    list_display = ("user", "name", "number", "media_api_key", "profile_picture", "provider", "session_name", "session_status", "created_at")
    search_fields = ("user__email", "name", "number", "media_api_key", "session_name", "provider")


@admin.register(WhatsAppChat)
class WhatsAppChatAdmin(admin.ModelAdmin):
    list_display = ("account", "chat_id", "name","profile_picture", "is_group", "last_message_at")
    search_fields = ("chat_id", "name")
    list_filter = ("is_group",)


@admin.register(WhatsAppMessage)
class WhatsAppMessageAdmin(admin.ModelAdmin):
    list_display = ("chat", "direction", "type", "sender_name", "ack", "ack_code", "timestamp")
    search_fields = ("message_id", "text", "sender_id")
    list_filter = ("direction", "type", "ack")


@admin.register(WhatsAppDeletedMessage)
class WhatsAppDeletedMessageAdmin(admin.ModelAdmin):
    list_display = ("account", "chat_id", "message_id", "deleted_at")
    search_fields = ("chat_id", "message_id", "account__user__email")
    readonly_fields = ("account", "chat_id", "message_id", "deleted_at")

