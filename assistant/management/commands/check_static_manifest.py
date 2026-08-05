"""python manage.py check_static_manifest

Fails loudly when the hashed static filenames `{% static %}` emits no longer
match what's on disk — i.e. when `staticfiles.json` is stale.

This exists because of a silent production failure that shipped a redesign
nobody could see. `manage.py` defaults DJANGO_SETTINGS_MODULE to
config.settings.development, so a deploy that runs a bare
`python manage.py collectstatic` uses the plain StaticFilesStorage instead of
the CompressedManifestStaticFilesStorage configured in production. That copies
the new CSS into STATIC_ROOT but never rehashes it, never rewrites
staticfiles.json, and never regenerates the .gz/.br siblings. Gunicorn, running
production settings, then reads the *old* manifest and serves the *old*
stylesheet — with no error anywhere, because the stale manifest entry still
resolves to a file that exists.

Run this straight after collectstatic in the deploy script. A non-zero exit
aborts the deploy (update.sh runs under `set -e`) rather than restarting
Gunicorn onto a stale manifest.

The freshness test is mtime-based on purpose: `git pull` gives every changed
file a new mtime, and collectstatic writes staticfiles.json last, so a manifest
older than any source file means collectstatic did not rebuild it this deploy.
Comparing file *contents* would be wrong here — Django rewrites url()
references while post-processing CSS, so a correctly collected stylesheet does
not match its source byte for byte.
"""

import os
from pathlib import Path

from django.apps import apps
from django.conf import settings
from django.contrib.staticfiles.finders import get_finders
from django.contrib.staticfiles.storage import staticfiles_storage
from django.core.management.base import BaseCommand, CommandError

FIX = (
    "DJANGO_SETTINGS_MODULE=config.settings.production "
    "python manage.py collectstatic --noinput"
)


class Command(BaseCommand):
    help = "Verify staticfiles.json is present and no older than the files it indexes."

    def handle(self, *args, **options):
        # A manifest backend is the whole point; plain StaticFilesStorage here
        # means the wrong settings module is in play.
        manifest_name = getattr(staticfiles_storage, "manifest_name", None)
        if manifest_name is None:
            # __class__ (not type()) so the lazy wrapper reports the real backend.
            backend = staticfiles_storage.__class__.__name__
            raise CommandError(
                f"Static storage is {backend}, which keeps no manifest — so nothing "
                f"rehashes files or rewrites the manifest, and {{% static %}} keeps "
                f"resolving to whatever was collected last.\n"
                f"Re-run: {FIX}"
            )

        static_root = Path(settings.STATIC_ROOT)
        manifest = static_root / manifest_name
        if not manifest.exists():
            raise CommandError(f"{manifest} is missing.\nRe-run: {FIX}")
        manifest_mtime = manifest.stat().st_mtime

        # base.py only adds BASE_DIR/"static" to STATICFILES_DIRS if it exists.
        # If it somehow doesn't, the finder sweep below quietly finds no project
        # assets and this command would pass while css/app.css went missing.
        if not settings.STATICFILES_DIRS:
            raise CommandError(
                f"STATICFILES_DIRS is empty, so {manifest_name} indexes no project assets. "
                f"Expected {settings.BASE_DIR / 'static'} to exist — is the checkout complete?"
            )

        # Enumerate exactly what collectstatic would, via the same finders and
        # the same ignore patterns. Walking the directories directly would flag
        # files collectstatic deliberately skips — the default patterns drop
        # dotfiles, so static/.gitkeep is never collected and is rightly absent
        # from the manifest.
        ignore_patterns = apps.get_app_config("staticfiles").ignore_patterns

        problems = []
        seen = set()
        for finder in get_finders():
            for path, storage in finder.list(ignore_patterns):
                prefix = getattr(storage, "prefix", None)
                name = os.path.join(prefix, path) if prefix else path
                if name in seen:  # collectstatic keeps the first finder to win
                    continue
                seen.add(name)
                try:
                    hashed = staticfiles_storage.stored_name(name)
                except ValueError:
                    problems.append(f"{name}: no entry in {manifest_name}")
                    continue
                if not (static_root / hashed).exists():
                    problems.append(f"{name}: manifest points at {hashed}, which is not on disk")
                elif os.path.getmtime(storage.path(path)) > manifest_mtime:
                    problems.append(f"{name}: modified after {manifest_name} was written")

        if problems:
            # A stale manifest usually implicates every collected file at once,
            # so keep the deploy log readable rather than printing all 100+.
            shown = problems[:10]
            if len(problems) > len(shown):
                shown.append(f"...and {len(problems) - len(shown)} more")
            raise CommandError(
                "Static manifest is out of date — templates would link stale assets:\n  "
                + "\n  ".join(shown)
                + f"\n\nRe-run: {FIX}"
            )

        self.stdout.write(
            self.style.SUCCESS(f"{manifest_name} is current ({len(seen)} files checked).")
        )
