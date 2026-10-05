from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('calling_agent', '0004_production_calling'),
    ]

    operations = [
        migrations.AddField(
            model_name='call',
            name='attempt_number',
            field=models.PositiveSmallIntegerField(default=1),
        ),
        migrations.AddField(
            model_name='call',
            name='trigger',
            field=models.CharField(
                choices=[
                    ('manual', 'Manual'),
                    ('scheduled', 'Scheduled'),
                ],
                default='manual',
                max_length=16,
            ),
        ),
        migrations.AddConstraint(
            model_name='call',
            constraint=models.UniqueConstraint(
                condition=models.Q(
                    ('lead__isnull', False),
                    ('scheduled_for__isnull', False),
                ),
                fields=('lead', 'scheduled_for', 'attempt_number'),
                name='unique_scheduled_call_attempt',
            ),
        ),
    ]
