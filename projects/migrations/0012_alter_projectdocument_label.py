from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('projects', '0011_project_visible_to_company'),
    ]

    operations = [
        migrations.AlterField(
            model_name='projectdocument',
            name='label',
            field=models.CharField(
                choices=[
                    ('brochure', 'Brochure'),
                    ('floor_plan', 'Floor Plan'),
                    ('fact_checks', 'Fact Checks'),
                ],
                max_length=255,
            ),
        ),
    ]