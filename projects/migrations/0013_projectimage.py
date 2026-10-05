import django.db.models.deletion
from django.db import migrations, models

import projects.models


class Migration(migrations.Migration):

    dependencies = [
        ('projects', '0012_alter_projectdocument_label'),
    ]

    operations = [
        migrations.CreateModel(
            name='ProjectImage',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('image', models.ImageField(upload_to=projects.models.project_gallery_upload_path)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('project', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='images', to='projects.project')),
            ],
            options={
                'ordering': ['created_at'],
            },
        ),
    ]