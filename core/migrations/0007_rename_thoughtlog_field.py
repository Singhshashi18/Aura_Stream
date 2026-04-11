from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0006_thoughtlog_interrupted_by_and_more"),
    ]

    operations = [
        migrations.RenameField(
            model_name="thoughtlog",
            old_name="thought_block",
            new_name="user_message",
        ),
    ]
