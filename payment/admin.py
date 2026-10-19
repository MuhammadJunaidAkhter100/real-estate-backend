from django.contrib import admin
from payment.models import CreditCharge, ProcessedStripeEvent, UsageCounter


@admin.register(ProcessedStripeEvent)
class ProcessedStripeEventAdmin(admin.ModelAdmin):
    list_display = ('event_id', 'event_type', 'processed_at')
    search_fields = ('event_id', 'event_type')
    readonly_fields = ('event_id', 'event_type', 'processed_at')


@admin.register(CreditCharge)
class CreditChargeAdmin(admin.ModelAdmin):
    list_display = ('task_id', 'company', 'kind', 'cost', 'created_at')
    list_filter = ('kind', 'created_at')
    search_fields = ('task_id', 'company__name')


@admin.register(UsageCounter)
class UsageCounterAdmin(admin.ModelAdmin):
    list_display = ('company', 'kind', 'period_start', 'used', 'updated_at')
    list_filter = ('kind', 'period_start')
    search_fields = ('company__name',)
