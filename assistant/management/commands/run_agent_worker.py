"""python manage.py run_agent_worker

Long-running worker for the Claude agent (lifeassistant-agent.service). The
Telegram webhook only queues messages; this process answers them. Stops
cleanly on SIGTERM after the current job.
"""

import signal
import time

from django.core.management.base import BaseCommand
from django.db import close_old_connections

from assistant.agent.worker import claim_next_jobs, requeue_orphaned_jobs, run_jobs

IDLE_POLL_SECONDS = 1.0


class Command(BaseCommand):
    help = "Answer queued agent messages until stopped."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Process at most one batch, then exit.")

    def handle(self, *args, **options):
        stopping = False

        def _stop(signum, frame):
            nonlocal stopping
            stopping = True

        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)

        requeued = requeue_orphaned_jobs()
        if requeued:
            self.stdout.write(f"Requeued {requeued} interrupted job(s).")
        self.stdout.write("Agent worker started.")
        while not stopping:
            close_old_connections()
            jobs = claim_next_jobs()
            if jobs:
                run_jobs(jobs)
            if options["once"]:
                break
            if not jobs:
                time.sleep(IDLE_POLL_SECONDS)
        close_old_connections()
        self.stdout.write("Agent worker stopped.")
