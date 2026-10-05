from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('projects', '0011_project_visible_to_company'),
        ('users', '0034_company_visible_projects'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='company',
            name='visible_projects',
        ),
    ]