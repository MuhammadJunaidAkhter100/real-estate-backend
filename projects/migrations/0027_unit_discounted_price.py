from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('projects', '0026_unit_floor_plan_image'),
    ]

    operations = [
        migrations.AddField(
            model_name='unit',
            name='discounted_price',
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                help_text='Discounted / Selling price (optional)',
                max_digits=12,
                null=True,
            ),
        ),
    ]
