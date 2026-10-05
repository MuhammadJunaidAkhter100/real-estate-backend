import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('calling_agent', '0005_scheduled_call_claims'),
        ('users', '0045_task_related_call'),
    ]

    operations = [
        migrations.CreateModel(
            name='CallGeneratedAction',
            fields=[
                (
                    'id',
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name='ID',
                    ),
                ),
                (
                    'action_type',
                    models.CharField(
                        choices=[
                            ('create_task', 'Create Task'),
                            ('update_lead', 'Update Lead'),
                            ('transfer_fallback', 'Transfer Fallback'),
                        ],
                        max_length=32,
                    ),
                ),
                ('title', models.CharField(max_length=255)),
                ('payload', models.JSONField(blank=True, default=dict)),
                ('idempotency_key', models.UUIDField(unique=True)),
                (
                    'status',
                    models.CharField(
                        choices=[
                            ('pending', 'Pending'),
                            ('completed', 'Completed'),
                            ('failed', 'Failed'),
                        ],
                        default='pending',
                        max_length=16,
                    ),
                ),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                (
                    'call',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='generated_actions',
                        to='calling_agent.call',
                    ),
                ),
                (
                    'task',
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name='generated_call_actions',
                        to='users.task',
                    ),
                ),
            ],
            options={
                'ordering': ['-created_at'],
                'indexes': [
                    models.Index(
                        fields=['call', 'action_type', '-created_at'],
                        name='calling_action_lookup_idx',
                    ),
                ],
            },
        ),
    ]
