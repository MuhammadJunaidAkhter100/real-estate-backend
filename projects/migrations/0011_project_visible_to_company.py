from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0034_company_visible_projects'),
        ('projects', '0010_projectdocument'),
    ]

    operations = [
        migrations.AddField(
            model_name='project',
            name='visible_to_company',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=models.SET_NULL,
                related_name='visible_projects',
                to='users.company',
            ),
        ),
    ]