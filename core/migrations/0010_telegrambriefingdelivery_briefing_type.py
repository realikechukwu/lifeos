from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0009_telegramupdate_processing_started_at"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="telegrambriefingdelivery",
            name="telegram_one_briefing_per_day",
        ),
        migrations.AddField(
            model_name="telegrambriefingdelivery",
            name="briefing_type",
            field=models.CharField(
                choices=[("daily", "Daily"), ("weekly", "Weekly"), ("monthly", "Monthly")],
                default="daily",
                max_length=10,
            ),
        ),
        migrations.AddConstraint(
            model_name="telegrambriefingdelivery",
            constraint=models.UniqueConstraint(
                fields=("user", "briefing_type", "briefing_date"),
                name="telegram_one_briefing_per_type_day",
            ),
        ),
    ]
