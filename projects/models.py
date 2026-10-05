from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models


def project_image_upload_path(instance, filename):
    title_slug = instance.title.replace(' ', '_').lower()
    return f'projects/{instance.id}_{title_slug}/images/{filename}'


def project_gallery_upload_path(instance, filename):
    title_slug = instance.project.title.replace(' ', '_').lower()
    return f'projects/{instance.project_id}_{title_slug}/gallery/{filename}'


def unit_floor_plan_upload_path(instance, filename):
    title_slug = instance.project.title.replace(' ', '_').lower()
    return f'projects/{instance.project_id}_{title_slug}/units/{instance.label}/{filename}'


class Project(models.Model):
    class Status(models.TextChoices):
        IN_PROGRESS = 'in_progress', 'In Progress'
        READY = 'ready', 'Ready'
        PLANNED = 'planned', 'Planned'

    class ProjectType(models.TextChoices):
        RESIDENTIAL = 'residential', 'Residential'
        COMMERCIAL = 'commercial', 'Commercial'
        HOSPITALITY = 'hospitality', 'Hospitality'

    class ProjectStatus(models.TextChoices):
        DRAFT = 'draft', 'Draft'
        LIVE = 'live', 'Live'

    title = models.CharField(max_length=255)
    associated_country = models.CharField(max_length=100, blank=True, default='')
    description = models.TextField()
    location = models.CharField(max_length=255)
    developer = models.CharField(max_length=255)
    estimated_completion = models.CharField(max_length=100, blank=True, default='')
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PLANNED)
    project_status = models.CharField(max_length=20,default=ProjectStatus.DRAFT)
    starting_price = models.DecimalField(max_digits=12, decimal_places=2)
    yield_percentage = models.DecimalField(max_digits=5, decimal_places=2)
    currency = models.CharField(max_length=3, blank=True, default='')
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='projects',
    )
    visible_to_companies = models.ManyToManyField(
        'users.Company',
        blank=True,
        related_name='visible_projects',
    )
    image = models.JSONField(default=list, blank=True)
    proposal_images = models.JSONField(default=dict, blank=True)
    project_type = models.CharField(max_length=20, choices=ProjectType.choices)
    property_category = models.CharField(max_length=100, blank=True, default='')
    # Cached unit counts. Kept in sync automatically by the Unit post_save /
    # post_delete signals (see projects/signals.py) and by the
    # `recalculate_project_unit_counts` command.
    number_of_units = models.PositiveIntegerField(null=True, blank=True, default=0)
    bed_1 = models.PositiveIntegerField(null=True, blank=True, default=0)
    bed_2 = models.PositiveIntegerField(null=True, blank=True, default=0)
    bed_3 = models.PositiveIntegerField(null=True, blank=True, default=0)
    studio = models.PositiveIntegerField(null=True, blank=True, default=0)

    proposal_cover_image = models.FileField(
        upload_to='project_proposal_assets/', blank=True, null=True,
    )
    proposal_logo_image = models.FileField(
        upload_to='project_proposal_assets/', blank=True, null=True,
    )
    proposal_ai_facts = models.JSONField(default=dict, blank=True)
    proposal_assets_fingerprint = models.CharField(max_length=64, blank=True, default='')

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def unit_count(self):
        return self.units.count()

    def recalculate_unit_counts(self, save=True):
        """
        Recompute number_of_units + bed_1 / bed_2 / bed_3 / studio from the
        related Unit rows and (optionally) persist them.
        """
        units = self.units.all()
        counts = {
            'number_of_units': units.count(),
            'bed_1': units.filter(category__icontains='1 bed').count(),
            'bed_2': units.filter(category__icontains='2 bed').count(),
            'bed_3': units.filter(category__icontains='3 bed').count(),
            'studio': units.filter(category__icontains='studio').count(),
        }
        for field, value in counts.items():
            setattr(self, field, value)
        if save:
            Project.objects.filter(pk=self.pk).update(**counts)
        return counts

    @property
    def current_promotion(self):
        from django.utils import timezone
        now = timezone.now()
        # Prioritize currently active promotion
        for promo in self.promotions.all():
            if promo.compute_status(now) == promo.Status.ACTIVE:
                return promo
        # Fallback to upcoming promotion (not expired)
        for promo in self.promotions.all():
            if promo.compute_status(now) == promo.Status.UPCOMING:
                return promo
        return None

    @property
    def has_active_promotion(self) -> bool:
        return self.current_promotion is not None

    @property
    def active_promotion_discount(self):
        promo = self.current_promotion
        return promo.discount if promo else None

    @property
    def promotion_title(self):
        promo = self.current_promotion
        return promo.title if promo else None

    @property
    def promotion_status(self):
        promo = self.current_promotion
        if promo:
            from django.utils import timezone
            return promo.compute_status(timezone.now())
        return None

    def __str__(self):
        return self.title


class ProjectAgentAssignment(models.Model):
    agent = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='project_assignments',
    )
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name='agent_assignments',
    )
    agent_split = models.DecimalField(max_digits=5, decimal_places=2)
    company_split = models.DecimalField(max_digits=5, decimal_places=2)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['agent', 'project'],
                name='unique_agent_project_assignment',
            ),
        ]

    def clean(self):
        if self.agent_split + self.company_split != 100:
            raise ValidationError("agent_split + company_split must equal 100.")

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.agent.email} → {self.project.title}"


class ProjectDocument(models.Model):
    LABEL_CHOICES = (
        ('brochure', 'Brochure'),
        ('floor_plan', 'Floor Plan'),
        ('fact_checks', 'Fact Checks'),
    )

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='documents')
    label = models.CharField(max_length=255, choices=LABEL_CHOICES)
    file = models.FileField(upload_to='project_documents/')

    # Cached text extracted from the uploaded PDF (brochure / floor plan / fact sheet).
    # Populated asynchronously after upload and used to answer chatbot questions.
    extracted_text = models.TextField(blank=True, default='')
    extracted_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.project.title} - {self.label}"



class Unit(models.Model):
    class UnitStatus(models.TextChoices):
        AVAILABLE = 'available', 'Available'
        RESERVED = 'reserved', 'Reserved'
        SOLD = 'sold', 'Sold'

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='units')
    associated_country = models.CharField(max_length=100, blank=True, default='')
    label = models.CharField(max_length=100)
    category = models.CharField(max_length=100, blank=True, default='', help_text='e.g., 1 Bed, 2 Bed, Studio')
    floor = models.CharField(max_length=100, blank=True, default='', help_text='e.g., Ground Floor, 1st Floor')
    area_m2 = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True, help_text='Area in square meters')
    area_ft2 = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True, help_text='Area in square feet')
    list_price = models.DecimalField(max_digits=12, decimal_places=2, help_text='Original/List price')
    discounted_price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, help_text='Discounted / Selling price (optional)')
    est_market_rent = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, help_text='Estimated market rent (PCM, optional)')
    est_yield_gross = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True, help_text='Estimated gross yield in % (optional)')
    currency = models.CharField(max_length=3, blank=True, default='')
    floor_plan_image = models.ImageField( upload_to=unit_floor_plan_upload_path, null=True, blank=True, help_text='Floor plan image for this unit')
    created_by = models.ForeignKey( settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='units')
    status = models.CharField(max_length=20, choices=UnitStatus.choices, default=UnitStatus.AVAILABLE)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['label']

    def __str__(self):
        return f'{self.project.title} - {self.label}'


class ZohoCredentials(models.Model):
    """
    Zoho CRM credentials store karne ke liye
    User ke saath link hai taki har user apne credentials rakh sake
    """
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='zoho_credentials'
    )
    access_token = models.TextField()
    refresh_token = models.TextField()
    zoho_user_id = models.CharField(max_length=255, blank=True)
    zoho_organization_id = models.CharField(max_length=255, blank=True)
    token_expires_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    
    class Meta:
        verbose_name_plural = "Zoho Credentials"
        ordering = ['-created_at']
    
    def is_token_expired(self):
        """Check if access token expired hai"""
        from django.utils import timezone

        if self.token_expires_at:
            return timezone.now() >= self.token_expires_at
        return False
    
    def __str__(self):
        return f"Zoho Credentials - {self.user.email}"


class Promotion(models.Model):
    class Status(models.TextChoices):
        UPCOMING = 'upcoming', 'Upcoming'
        ACTIVE = 'active', 'Active'
        EXPIRED = 'expired', 'Expired'

    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name='promotions',
    )
    title = models.CharField(max_length=255, help_text='Promotion title or type')
    discount = models.PositiveIntegerField(help_text='Discount percentage, e.g. 10 for 10%')
    start_date = models.DateTimeField()
    end_date = models.DateTimeField()
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.UPCOMING,
    )
    original_prices = models.JSONField(default=dict, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='promotions',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.title} - {self.project.title} ({self.discount}%)"

    def normalize_end_date(self):
        if self.end_date and self.end_date.hour == 0 and self.end_date.minute == 0 and self.end_date.second == 0:
            self.end_date = self.end_date.replace(hour=23, minute=59, second=59)

    def clean(self):
        super().clean()
        self.normalize_end_date()

    def save(self, *args, **kwargs):
        self.normalize_end_date()
        super().save(*args, **kwargs)

    def compute_status(self, now=None):
        from django.utils import timezone
        if now is None:
            now = timezone.now()
        
        end_dt = self.end_date
        if end_dt and end_dt.hour == 0 and end_dt.minute == 0 and end_dt.second == 0:
            end_dt = end_dt.replace(hour=23, minute=59, second=59)

        if self.start_date > now:
            return self.Status.UPCOMING
        elif self.start_date <= now <= end_dt:
            return self.Status.ACTIVE
        else:
            return self.Status.EXPIRED

    def activate(self):
        from decimal import Decimal
        available_units = self.project.units.filter(status=Unit.UnitStatus.AVAILABLE)
        snapshot = {}
        for unit in available_units:
            orig_disc = float(unit.discounted_price) if unit.discounted_price is not None else None
            snapshot[str(unit.id)] = orig_disc

            # Determine base price: use discounted_price if available, else list_price
            if unit.discounted_price is not None and unit.discounted_price > 0:
                base_price = unit.discounted_price
            else:
                base_price = unit.list_price

            discount_factor = Decimal('1') - (Decimal(str(self.discount)) / Decimal('100'))
            new_price = round(Decimal(str(base_price)) * discount_factor, 2)

            unit.discounted_price = new_price
            unit.save(update_fields=['discounted_price', 'updated_at'])

        self.original_prices = snapshot
        self.status = self.Status.ACTIVE

    def deactivate(self, target_status=Status.EXPIRED):
        from decimal import Decimal
        if self.original_prices:
            for unit_id_str, orig_val in self.original_prices.items():
                try:
                    unit = Unit.objects.get(pk=int(unit_id_str))
                    if orig_val is None:
                        unit.discounted_price = None
                    else:
                        unit.discounted_price = Decimal(str(orig_val))
                    unit.save(update_fields=['discounted_price', 'updated_at'])
                except Unit.DoesNotExist:
                    pass
        self.original_prices = {}
        self.status = target_status

    def sync_status(self, now=None):
        if self.end_date and self.end_date.hour == 0 and self.end_date.minute == 0 and self.end_date.second == 0:
            self.end_date = self.end_date.replace(hour=23, minute=59, second=59)
            self.save(update_fields=['end_date', 'status', 'original_prices', 'updated_at'])

        expected = self.compute_status(now=now)
        current = self.status

        if expected == self.Status.ACTIVE and current != self.Status.ACTIVE:
            self.activate()
            self.save(update_fields=['original_prices', 'status', 'updated_at'])
        elif expected != self.Status.ACTIVE and current == self.Status.ACTIVE:
            self.deactivate(target_status=expected)
            self.save(update_fields=['original_prices', 'status', 'updated_at'])
        elif current != expected:
            self.status = expected
            self.save(update_fields=['status', 'updated_at'])

    @classmethod
    def sync_all_promotions(cls):
        from django.utils import timezone
        now = timezone.now()
        promotions = cls.objects.all()
        for promo in promotions:
            promo.sync_status(now=now)

