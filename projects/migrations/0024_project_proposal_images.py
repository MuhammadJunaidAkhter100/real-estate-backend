from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('projects', '0023_project_proposal_ai_facts_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='project',
            name='proposal_images',
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
