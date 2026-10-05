from django.contrib import admin

from users.models import AgentCommission, Company, Lead, Task, User

# Register your models here.

admin.site.register(User)
admin.site.register(Company)


@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'description', 'priority', 'status', 'due_date', 'created_by', 'created_at')
    list_filter = ('priority', 'status', 'due_date')
    search_fields = ('name', 'description', 'created_by__email')
    ordering = ('-created_at',)


@admin.register(Lead)
class LeadAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'status', 'is_assign', 'source', 'email', 'phone_no', 'scheduled_at', 'created_by', 'created_at')
    list_filter = ('status', 'is_assign', 'source', 'scheduled_at')
    search_fields = ('name', 'email', 'phone_no', 'created_by__email')
    ordering = ('-created_at',)


@admin.register(AgentCommission)
class AgentCommissionAdmin(admin.ModelAdmin):
    list_display = ('id', 'agent', 'lead', 'project', 'unit', 'list_price', 'agent_commission', 'company_commission', 'created_at')
    list_filter = ('created_at', 'project')
    search_fields = ('agent__email', 'lead__name', 'project__title')
    ordering = ('-created_at',)
    readonly_fields = ('created_at', 'updated_at')