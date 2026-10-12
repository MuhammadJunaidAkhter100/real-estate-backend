from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0059_company_website_company_website_data_company_website_scrape_status'),
    ]

    operations = [
        migrations.AddField(
            model_name='company',
            name='phone',
            field=models.CharField(blank=True, default='', max_length=32),
        ),
    ]