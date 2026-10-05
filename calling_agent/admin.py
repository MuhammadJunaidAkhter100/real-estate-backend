from django.contrib import admin

from calling_agent.models import Call, CallGeneratedAction


@admin.register(Call)
class CallAdmin(admin.ModelAdmin):
    list_display = (
        'id',
        'public_id',
        'lead_name',
        'phone_number',
        'status',
        'trigger',
        'scheduled_for',
        'duration',
        'provider_conversation_id',
        'created_at',
    )
    list_filter = ('status', 'direction', 'trigger', 'created_at')
    search_fields = (
        'public_id',
        'lead_name',
        'phone_number',
        'provider_conversation_id',
        'provider_call_id',
        'summary',
    )
    readonly_fields = (
        'public_id',
        'initiation_key',
        'created_at',
        'updated_at',
    )


@admin.register(CallGeneratedAction)
class CallGeneratedActionAdmin(admin.ModelAdmin):
    list_display = (
        'id',
        'call',
        'action_type',
        'status',
        'task',
        'created_at',
    )
    list_filter = ('action_type', 'status', 'created_at')
    search_fields = (
        'call__public_id',
        'idempotency_key',
        'title',
    )
    readonly_fields = (
        'idempotency_key',
        'created_at',
        'updated_at',
    )
