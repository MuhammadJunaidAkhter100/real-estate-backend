from django.contrib import admin

from chatbot.models import ChatSession, KnowledgeBaseDocument


@admin.register(ChatSession)
class ChatSessionAdmin(admin.ModelAdmin):
    list_display = ('id', 'user', 'thread_id', 'title', 'created_at', 'updated_at')
    list_filter = ('created_at',)
    search_fields = ('user__email', 'title', 'thread_id')
    readonly_fields = ('thread_id', 'created_at', 'updated_at')


@admin.register(KnowledgeBaseDocument)
class KnowledgeBaseDocumentAdmin(admin.ModelAdmin):
    list_display = ('id', 'user', 'original_filename', 'file_type', 'associated_country', 'status', 'created_at')
    list_filter = ('status', 'file_type', 'associated_country', 'created_at')
    search_fields = ('user__email', 'original_filename', 'task_id')
    readonly_fields = ('task_id', 'created_at', 'updated_at')
