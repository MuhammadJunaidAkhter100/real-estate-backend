from django.db import migrations


def backfill_is_assign(apps, schema_editor):
    Lead = apps.get_model('users', 'Lead')
    Lead.objects.filter(assigned_to__isnull=False).update(is_assign=True)
    Lead.objects.filter(assigned_to__isnull=True).update(is_assign=False)


def reverse_noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0043_lead_is_assign'),
    ]

    operations = [
        migrations.RunPython(backfill_is_assign, reverse_noop),
    ]
