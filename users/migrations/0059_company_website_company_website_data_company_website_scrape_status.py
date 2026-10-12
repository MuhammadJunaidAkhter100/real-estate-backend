from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0058_company_billing_fields'),
    ]

    operations = [
        migrations.AddField(
            model_name='company',
            name='website',
            field=models.URLField(blank=True, default='', max_length=255),
        ),
        migrations.AddField(
            model_name='company',
            name='website_data',
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name='company',
            name='website_scrape_status',
            field=models.CharField(blank=True, default='', max_length=30),
        ),
    ]
