from django.contrib import admin

from .models import GeneratedProposal


@admin.register(GeneratedProposal)
class GeneratedProposalAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "project",
        "unit",
        "lead",
        "status",
        "created_at",
        "updated_at",
    )
    list_filter = ("status", "created_at", "updated_at")
    search_fields = ("project__name", "lead__email", "unit__label")
    readonly_fields = ("created_at", "updated_at", "task_id")
    fieldsets = (
        (
            "Proposal Info",
            {
                "fields": (
                    "project",
                    "unit",
                    "lead",
                    "generated_by",
                    "status",
                )
            },
        ),
        (
            "Files & URLs",
            {"fields": ("file", "hosted_url")},
        ),
        (
            "AI Data",
            {"fields": ("ai_facts",)},
        ),
        (
            "Error & Task",
            {"fields": ("error", "task_id")},
        ),
        (
            "Timestamps",
            {"fields": ("created_at", "updated_at"), "classes": ("collapse",)},
        ),
    )
