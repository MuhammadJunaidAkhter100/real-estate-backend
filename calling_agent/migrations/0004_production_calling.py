import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def delete_prototype_calls(apps, schema_editor):
    Call = apps.get_model('calling_agent', 'Call')
    Call.objects.all().delete()


class Migration(migrations.Migration):
    atomic = False


    dependencies = [
        ('calling_agent', '0003_remove_call_customer_name_alter_call_status'),
        ('users', '0045_task_related_call'),
    ]

    operations = [
        migrations.RunPython(
            delete_prototype_calls,
            reverse_code=migrations.RunPython.noop,
        ),
        migrations.RemoveField(
            model_name='call',
            name='duration',
        ),
        migrations.RemoveField(
            model_name='call',
            name='recording_url',
        ),
        migrations.AddField(
            model_name='call',
            name='public_id',
            field=models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
        ),
        migrations.AddField(
            model_name='call',
            name='company',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='calls',
                to='users.company',
            ),
        ),
        migrations.AddField(
            model_name='call',
            name='lead',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='calls',
                to='users.lead',
            ),
        ),
        migrations.AddField(
            model_name='call',
            name='context_user',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='context_calls',
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name='call',
            name='outbound_number',
            field=models.CharField(blank=True, default='', max_length=32),
        ),
        migrations.AddField(
            model_name='call',
            name='direction',
            field=models.CharField(
                choices=[('outbound', 'Outbound')],
                default='outbound',
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name='call',
            name='agent_config_key',
            field=models.CharField(default='default', max_length=64),
        ),
        migrations.AddField(
            model_name='call',
            name='provider_agent_id',
            field=models.CharField(blank=True, default='', max_length=255),
        ),
        migrations.AddField(
            model_name='call',
            name='provider_phone_number_id',
            field=models.CharField(blank=True, default='', max_length=255),
        ),
        migrations.AddField(
            model_name='call',
            name='provider_conversation_id',
            field=models.CharField(blank=True, max_length=255, null=True, unique=True),
        ),
        migrations.AddField(
            model_name='call',
            name='provider_call_id',
            field=models.CharField(blank=True, default='', max_length=255),
        ),
        migrations.AddField(
            model_name='call',
            name='initiation_key',
            field=models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
        ),
        migrations.AddField(
            model_name='call',
            name='scheduled_for',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='call',
            name='claimed_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='call',
            name='claim_expires_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='call',
            name='cancelled_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='call',
            name='initiated_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='call',
            name='answered_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='call',
            name='ended_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='call',
            name='duration_seconds',
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='call',
            name='failure_code',
            field=models.CharField(blank=True, default='', max_length=64),
        ),
        migrations.AddField(
            model_name='call',
            name='failure_detail',
            field=models.CharField(blank=True, default='', max_length=500),
        ),
        migrations.AddField(
            model_name='call',
            name='recording_storage_key',
            field=models.CharField(blank=True, default='', max_length=1000),
        ),
        migrations.AddField(
            model_name='call',
            name='recording_content_type',
            field=models.CharField(blank=True, default='', max_length=100),
        ),
        migrations.AddField(
            model_name='call',
            name='recording_size_bytes',
            field=models.PositiveBigIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='call',
            name='recording_available',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='call',
            name='recording_fetched_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='call',
            name='provider_analysis',
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AlterField(
            model_name='call',
            name='lead_name',
            field=models.CharField(blank=True, default='', max_length=255),
        ),
        migrations.AlterField(
            model_name='call',
            name='phone_number',
            field=models.CharField(blank=True, default='', max_length=32),
        ),
        migrations.AlterField(
            model_name='call',
            name='status',
            field=models.CharField(
                choices=[
                    ('scheduled', 'Scheduled'),
                    ('claimed', 'Claimed'),
                    ('initiating', 'Initiating'),
                    ('initiation_unknown', 'Initiation Unknown'),
                    ('ringing', 'Ringing'),
                    ('in_progress', 'In Progress'),
                    ('completed', 'Completed'),
                    ('failed', 'Failed'),
                    ('cancelled', 'Cancelled'),
                    ('no_answer', 'No Answer'),
                    ('busy', 'Busy'),
                ],
                default='scheduled',
                max_length=32,
            ),
        ),
        migrations.AlterField(
            model_name='call',
            name='company',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name='calls',
                to='users.company',
            ),
        ),
        migrations.AddIndex(
            model_name='call',
            index=models.Index(
                fields=['status', 'scheduled_for'],
                name='calling_status_sched_idx',
            ),
        ),
        migrations.AddIndex(
            model_name='call',
            index=models.Index(
                fields=['company', '-created_at'],
                name='calling_company_created_idx',
            ),
        ),
        migrations.AddIndex(
            model_name='call',
            index=models.Index(
                fields=['lead', '-created_at'],
                name='calling_lead_created_idx',
            ),
        ),
        migrations.AddIndex(
            model_name='call',
            index=models.Index(
                fields=['provider_call_id'],
                name='calling_provider_call_idx',
            ),
        ),
    ]
