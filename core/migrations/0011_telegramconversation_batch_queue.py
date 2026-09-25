from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0010_telegrambriefingdelivery_briefing_type"),
    ]

    operations = [
        migrations.AddField(
            model_name="telegramconversation",
            name="pending_extractions",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name="telegramconversation",
            name="batch_size",
            field=models.PositiveSmallIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="telegramconversation",
            name="batch_index",
            field=models.PositiveSmallIntegerField(default=0),
        ),
    ]
