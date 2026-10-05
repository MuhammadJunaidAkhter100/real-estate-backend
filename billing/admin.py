from django.contrib import admin

from billing.models import CreditCharge, ProcessedStripeEvent, UsageCounter


@admin.register(ProcessedStripeEvent)
class ProcessedStripeEventAdmin(admin.ModelAdmin):
    list_display = ('event_id', 'event_type', 'processed_at')
    search_fields = ('event_id', 'event_type')


@admin.register(CreditCharge)
class CreditChargeAdmin(admin.ModelAdmin):
    list_display = ('task_id', 'company', 'kind', 'cost', 'created_at')
    list_filter = ('kind',)
    search_fields = ('task_id',)


@admin.register(UsageCounter)
class UsageCounterAdmin(admin.ModelAdmin):
    list_display = ('company', 'kind', 'period_start', 'used', 'updated_at')
    list_filter = ('kind',)