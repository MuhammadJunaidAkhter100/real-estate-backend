from django.db import migrations, models


def move_project_images_to_json(apps, schema_editor):
    Project = apps.get_model('projects', 'Project')
    ProjectImage = apps.get_model('projects', 'ProjectImage')

    for project in Project.objects.all():
        image_paths = []
        if project.image:
            image_paths.append(str(project.image))

        image_paths.extend(
            str(path)
            for path in ProjectImage.objects.filter(project_id=project.id).values_list('image', flat=True)
            if path
        )

        project.image_list = image_paths
        project.save(update_fields=['image_list'])


class Migration(migrations.Migration):

    dependencies = [
        ('projects', '0013_projectimage'),
    ]

    operations = [
        migrations.AddField(
            model_name='project',
            name='image_list',
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.RunPython(move_project_images_to_json, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name='project',
            name='image',
        ),
        migrations.RenameField(
            model_name='project',
            old_name='image_list',
            new_name='image',
        ),
        migrations.DeleteModel(
            name='ProjectImage',
        ),
    ]