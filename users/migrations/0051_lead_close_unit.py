from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0050_rename_users_lead_do_not_c_idx_users_lead_do_not__c099ff_idx'),
    ]

    operations = [
        migrations.AddField(
            model_name='lead',
            name='close_unit',
            field=models.JSONField(blank=True, default=dict, help_text="Unit details e.g. {'floor': 'Ground Floor', 'type': '2 bed', 'price': 500000}"),
        ),
    ]
