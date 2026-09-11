from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0008_patchworkshift_labels_and_suppression")]

    operations = [
        migrations.AddField(
            model_name="telegramupdate",
            name="processing_started_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
