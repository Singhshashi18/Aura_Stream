from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0003_product'),
    ]

    operations = [
        migrations.DeleteModel(
            name='Product',
        ),
    ]
