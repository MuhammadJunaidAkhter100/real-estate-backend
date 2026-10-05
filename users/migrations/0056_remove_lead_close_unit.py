from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0055_lead_units'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='lead',
            name='close_unit',
        ),
    ]
