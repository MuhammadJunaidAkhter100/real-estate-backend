from django.db import migrations, models


DEFAULT_COSTS = [
    ('chat_turn', 'query', 5),
    ('ai_proposal', 'proposal', 20),
    ('calling_agent_call', 'minute', 20),
    ('transcript_processing', 'transcript', 1),
    ('kb_document_extraction', 'document', 1),
]


def seed_credit_costs(apps, schema_editor):
    CreditCost = apps.get_model('payment', 'CreditCost')
    for operation, unit, credits in DEFAULT_COSTS:
        CreditCost.objects.update_or_create(
            operation=operation,
            defaults={'unit': unit, 'credits_per_unit': credits},
        )


def remove_credit_costs(apps, schema_editor):
    CreditCost = apps.get_model('payment', 'CreditCost')
    CreditCost.objects.filter(
        operation__in=[operation for operation, _unit, _credits in DEFAULT_COSTS]
    ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ('payment', '0006_stripeplanprice'),
    ]

    operations = [
        migrations.CreateModel(
            name='CreditCost',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('operation', models.CharField(choices=[('chat_turn', 'Chat query'), ('ai_proposal', 'Proposal generation'), ('calling_agent_call', 'Calling agent minute'), ('transcript_processing', 'Transcript processing'), ('kb_document_extraction', 'Knowledge document extraction')], max_length=50, unique=True)),
                ('unit', models.CharField(max_length=30)),
                ('credits_per_unit', models.PositiveIntegerField(default=1)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'verbose_name': 'AI credit cost',
                'verbose_name_plural': 'AI credit costs',
                'ordering': ['operation'],
            },
        ),
        migrations.RunPython(seed_credit_costs, remove_credit_costs),
    ]