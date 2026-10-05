from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0056_remove_lead_close_unit'),
    ]

    operations = [
        migrations.AddField(
            model_name='task',
            name='open_ended',
            field=models.BooleanField(
                default=False,
                help_text='When true, task is not auto-expired by the expiry cron.',
            ),
        ),
    ]
