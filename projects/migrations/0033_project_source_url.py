from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('projects', '0032_promotion'),
    ]

    operations = [
        migrations.AddField(
            model_name='project',
            name='source_url',
            field=models.URLField(blank=True, default='', max_length=2048),
        ),
    ]