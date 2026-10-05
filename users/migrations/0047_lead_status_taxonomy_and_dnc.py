from django.db import migrations, models


LEGACY_STATUS_MAP = {
    'qualified': 'interested',
    'in_negotiation': 'negotiation_ongoing',
}


STATUS_CHOICES = [
    ('new', 'New Lead'),
    ('call_pending', 'Call Pending'),
    ('contacted', 'Contacted'),
    ('no_answer', 'No Answer'),
    ('call_back_requested', 'Call Back Requested'),
    ('wrong_number', 'Wrong Number'),
    ('unreachable', 'Unreachable'),
    ('interested', 'Interested'),
    ('highly_interested', 'Highly Interested'),
    ('need_more_information', 'Need More Information'),
    ('site_visit_requested', 'Site Visit Requested'),
    ('budget_mismatch', 'Budget Mismatch'),
    ('location_mismatch', 'Location Mismatch'),
    ('not_interested', 'Not Interested'),
    ('follow_up_required', 'Follow-up Required'),
    ('brochure_sent', 'Brochure Sent'),
    ('whatsapp_follow_up', 'WhatsApp Follow-up'),
    ('email_sent', 'Email Sent'),
    ('site_visit_scheduled', 'Site Visit Scheduled'),
    ('site_visit_completed', 'Site Visit Completed'),
    ('negotiation_ongoing', 'Negotiation Ongoing'),
    ('documentation_in_progress', 'Documentation in Progress'),
    ('booking_amount_received', 'Booking Amount Received'),
    ('unit_reserved', 'Unit Reserved'),
    ('converted_won', 'Converted / Won'),
    ('lost_to_competitor', 'Lost to Competitor'),
    ('lost_no_response', 'Lost - No Response'),
    ('lost_budget_issue', 'Lost - Budget Issue'),
    ('future_prospect', 'Future Prospect'),
    ('duplicate_lead', 'Duplicate Lead'),
]


def forwards_migrate_legacy_statuses(apps, schema_editor):
    Lead = apps.get_model('users', 'Lead')
    for old_status, new_status in LEGACY_STATUS_MAP.items():
        Lead.objects.filter(status=old_status).update(status=new_status)


def backwards_migrate_legacy_statuses(apps, schema_editor):
    Lead = apps.get_model('users', 'Lead')
    reverse_map = {v: k for k, v in LEGACY_STATUS_MAP.items()}
    for new_status, old_status in reverse_map.items():
        Lead.objects.filter(status=new_status).update(status=old_status)


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0046_task_callback_scheduling'),
    ]

    operations = [
        migrations.AlterField(
            model_name='lead',
            name='status',
            field=models.CharField(
                choices=STATUS_CHOICES,
                default='new',
                max_length=40,
            ),
        ),
        migrations.RunPython(
            forwards_migrate_legacy_statuses,
            backwards_migrate_legacy_statuses,
        ),
        migrations.AddField(
            model_name='lead',
            name='do_not_contact',
            field=models.BooleanField(default=False),
        ),
        migrations.AddIndex(
            model_name='lead',
            index=models.Index(
                fields=['do_not_contact'],
                name='users_lead_do_not_c_idx',
            ),
        ),
    ]
