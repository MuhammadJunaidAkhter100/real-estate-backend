from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('projects', '0030_unit_est_market_rent_unit_est_yield_gross'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='ZohoCredentials',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('access_token', models.TextField()),
                ('refresh_token', models.TextField()),
                ('zoho_user_id', models.CharField(blank=True, max_length=255)),
                ('zoho_organization_id', models.CharField(blank=True, max_length=255)),
                ('token_expires_at', models.DateTimeField(blank=True, null=True)),
                ('is_active', models.BooleanField(default=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('last_synced_at', models.DateTimeField(blank=True, null=True)),
                ('user', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='zoho_credentials', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name_plural': 'Zoho Credentials',
                'ordering': ['-created_at'],
            },
        ),
    ]
