from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('projects', '0032_promotion'),
        ('users', '0052_agentcommission'),
    ]

    operations = [
        migrations.AddField(
            model_name='lead',
            name='unit',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='leads', to='projects.unit'),
        ),
    ]
