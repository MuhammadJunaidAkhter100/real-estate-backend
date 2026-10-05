from django.contrib import admin

from projects.models import Project, ProjectAgentAssignment, ProjectDocument, Promotion, Unit, ZohoCredentials


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ('id', 'title', 'project_type', 'status', 'developer', 'unit_count', 'starting_price', 'currency', 'created_at')
    list_filter = ('project_type', 'status', 'currency')
    search_fields = ('title', 'developer', 'location')
    ordering = ('-created_at',)
    readonly_fields = ('created_at', 'updated_at')
    fieldsets = (
        ('Basic Information', {
            'fields': (
                'title', 'description', 'associated_country', 'location',
                'developer', 'status', 'project_status', 'project_type',
                'property_category','estimated_completion'
            ),
        }),
        ('Pricing & Units', {
            'fields': (
                'starting_price', 'yield_percentage', 'currency',
                'number_of_units', 'bed_1', 'bed_2', 'bed_3', 'studio',
            ),
        }),
        ('Media', {
            'fields': ('image', 'proposal_images'),
        }),
        ('Proposal Assets', {
            'fields': (
                'proposal_cover_image', 'proposal_logo_image',
                'proposal_ai_facts', 'proposal_assets_fingerprint',
            ),
        }),
        ('Ownership & Visibility', {
            'fields': ('created_by', 'visible_to_companies'),
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
        }),
    )

    @admin.display(description='Units')
    def unit_count(self, obj):
        return obj.units.count()


@admin.register(Unit)
class UnitAdmin(admin.ModelAdmin):
    list_display = ('id', 'label', 'project', 'category', 'floor', 'area_ft2', 'list_price', 'status', 'created_at')
    list_filter = ('status', 'category', 'floor')
    search_fields = ('label', 'category', 'floor', 'project__title')
    ordering = ('project', 'label')


@admin.register(ProjectDocument)
class ProjectDocumentAdmin(admin.ModelAdmin):
    list_display = ('id', 'project', 'label', 'file', 'extracted_at', 'created_at')
    list_filter = ('label', 'extracted_at', 'created_at')
    search_fields = ('project__title', 'label')
    ordering = ('-created_at',)
    readonly_fields = ('extracted_text', 'extracted_at', 'created_at', 'updated_at')


admin.site.register(ProjectAgentAssignment)


@admin.register(ZohoCredentials)
class ZohoCredentialsAdmin(admin.ModelAdmin):
    list_display = ('id', 'user', 'is_active', 'is_token_expired', 'last_synced_at', 'created_at')
    list_filter = ('is_active', 'created_at', 'last_synced_at')
    search_fields = ('user__email', 'user__first_name', 'user__last_name', 'zoho_user_id')
    ordering = ('-created_at',)
    readonly_fields = ('created_at', 'updated_at', 'access_token', 'refresh_token')
    
    fieldsets = (
        ('User', {
            'fields': ('user',),
        }),
        ('Tokens', {
            'fields': ('access_token', 'refresh_token', 'token_expires_at'),
            'classes': ('collapse',),
        }),
        ('Zoho Information', {
            'fields': ('zoho_user_id', 'zoho_organization_id'),
        }),
        ('Status', {
            'fields': ('is_active', 'last_synced_at'),
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
        }),
    )
    
    def is_token_expired(self, obj):
        if obj.is_token_expired():
            return "⚠️  Expired"
        return "✅ Active"
    is_token_expired.short_description = "Token Status"


@admin.register(Promotion)
class PromotionAdmin(admin.ModelAdmin):
    list_display = ('id', 'title', 'project', 'discount', 'start_date', 'end_date', 'status', 'created_by', 'created_at')
    list_filter = ('status', 'created_at', 'start_date', 'end_date')
    search_fields = ('title', 'project__title', 'created_by__email', 'created_by__first_name', 'created_by__last_name')
    ordering = ('-created_at',)
    readonly_fields = ('created_at', 'updated_at', 'original_prices')

    fieldsets = (
        ('Basic Information', {
            'fields': ('project', 'title', 'discount', 'status'),
        }),
        ('Schedule', {
            'fields': ('start_date', 'end_date'),
        }),
        ('Details & Snapshot', {
            'fields': ('created_by', 'original_prices'),
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
        }),
    )

