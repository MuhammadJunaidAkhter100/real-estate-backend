from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0053_lead_unit'),
    ]

    operations = [
        migrations.AddField(
            model_name='task',
            name='description',
            field=models.TextField(blank=True, default=''),
        ),
    ]
