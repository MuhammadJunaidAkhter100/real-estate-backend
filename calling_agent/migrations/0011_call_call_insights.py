from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('calling_agent', '0010_remove_otel_event_type'),
    ]

    operations = [
        migrations.AddField(
            model_name='call',
            name='call_insights',
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text='Structured post-call insights: objections, preferences, etc.',
            ),
        ),
    ]
