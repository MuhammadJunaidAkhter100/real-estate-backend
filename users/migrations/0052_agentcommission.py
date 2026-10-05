from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('projects', '0032_promotion'),
        ('users', '0051_lead_close_unit'),
    ]

    operations = [
        migrations.CreateModel(
            name='AgentCommission',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('unit_details', models.JSONField(blank=True, default=dict)),
                ('list_price', models.DecimalField(decimal_places=2, max_digits=15)),
                ('agent_split', models.DecimalField(decimal_places=2, max_digits=5)),
                ('company_split', models.DecimalField(decimal_places=2, max_digits=5)),
                ('agent_commission', models.DecimalField(decimal_places=2, max_digits=15)),
                ('company_commission', models.DecimalField(decimal_places=2, max_digits=15)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('agent', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='agent_commissions', to=settings.AUTH_USER_MODEL)),
                ('lead', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='commissions', to='users.lead')),
                ('project', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='commissions', to='projects.project')),
                ('unit', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='commissions', to='projects.unit')),
            ],
            options={
                'ordering': ['-created_at'],
            },
        ),
        migrations.AddConstraint(
            model_name='agentcommission',
            constraint=models.UniqueConstraint(fields=('lead',), name='unique_lead_commission'),
        ),
    ]
