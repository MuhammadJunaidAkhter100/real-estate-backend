from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0064_supportticket'),
    ]

    operations = [
        migrations.AddField(
            model_name='user',
            name='facebook',
            field=models.URLField(blank=True, default='', max_length=255),
        ),
        migrations.AddField(
            model_name='user',
            name='instagram',
            field=models.URLField(blank=True, default='', max_length=255),
        ),
        migrations.AddField(
            model_name='user',
            name='linkedin',
            field=models.URLField(blank=True, default='', max_length=255),
        ),
        migrations.AddField(
            model_name='user',
            name='twitter',
            field=models.URLField(blank=True, default='', max_length=255),
        ),
        migrations.AddField(
            model_name='user',
            name='website',
            field=models.URLField(blank=True, default='', max_length=255),
        ),
    ]
