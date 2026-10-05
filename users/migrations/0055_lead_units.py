from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('projects', '0032_promotion'),
        ('users', '0054_task_description'),
    ]

    operations = [
        migrations.AddField(
            model_name='lead',
            name='units',
            field=models.ManyToManyField(blank=True, related_name='leads_multi', to='projects.unit'),
        ),
    ]
