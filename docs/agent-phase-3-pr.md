## Summary

Stacked on `agent-phase-2` (PR #2); this PR also targets `main` as requested.
Review the additional phase 3 commit for this layer. Do not merge or deploy.

Route authorised adult emails into idempotent worker jobs, retaining one agent
history per Gmail thread without idle rollover. Preserve the original poller
when the new flag is off, including unauthorised handling and labels. Ignore
self-sent, automated and bulk/list/junk mail to prevent loops.

Separate the sender's own words from untrusted quoted/forwarded content. Pass
PDF/JPEG/PNG attachments as native blocks, bounded to 10 MB combined. Clear
creates and task completions execute through phase 2's services immediately.
Other mutations require a subsequent YES (or YES <id>) from the same requester,
in the same thread, for a proposal actually offered in an earlier reply.
Python rejects confirmations contained only in quoted text.

Replies use the real outer sender, optional existing household-only CC rules,
Gmail thread id and RFC Message-ID reply headers. Record actual tool results
in the reply and sign LifeOS. Persist replies before delivery so retries do
not rerun tools. Successful processing labels Processed; permanent worker
failures label Failed and attempt a short apology to authorised humans only.

## Settings, migrations, dependencies

- New `AGENT_EMAIL_ENABLED=false`; phase 2's proposal flag still controls writes.
- Migration `0014_agent_email`: email thread identity, job input/delivery state,
  incoming-email FK, offered-proposal FK, Message-ID and loop-guard headers.
- No new packages; Anthropic remains pinned to 1.14.0.

## Exact server steps (owner only; none run here)

After the normal reviewed deployment applies migration 0014, add
`AGENT_EMAIL_ENABLED=true` to the shared environment file. Keep
`AGENT_PROPOSALS_ENABLED=true`; verify `ANTHROPIC_API_KEY`,
`GOOGLE_ASSISTANT_EMAIL`, `AUTHORISED_EMAIL_IKE` and `AUTHORISED_EMAIL_WIFE`.
For Telegram keep `AGENT_ENABLED=true` too. Run:

```bash
sudo systemctl restart lifeassistant-web lifeassistant-agent
```

The existing Gmail oneshot loads the environment on its next timer invocation;
no timer/unit changes and no additional Gmail oneshot restart are needed.

## Manual checklist

- [ ] Send an email creating a task; receive a signed reply in the same thread.
- [ ] Reply in that thread; the agent remembers the earlier exchange.
- [ ] Forward an appointment with a clear instruction; confirm the outer sender receives the reply.
- [ ] Attach PDF/JPEG/PNG; other/oversized files are named without being passed.
- [ ] Complete a task by email; it completes immediately and reports the actual result.
- [ ] Request event/reminder cancellation; nothing changes until a later YES.
- [ ] Quoted YES, another person's YES and another thread's YES cannot confirm.
- [ ] With several pending proposals, reply YES <id> to identify one.
- [ ] Ask to send to an arbitrary address; no mail reaches that address.
- [ ] Explicit household CC reaches only the other configured adult.
- [ ] Automated/self-sent mail creates no agent conversation; unauthorised handling persists.
- [ ] Turn the flag off; the original email extraction route is restored.

## Validation

`DATABASE_URL= .venv/bin/python manage.py test --noinput`: all 270 tests pass.
`makemigrations --check --dry-run` detects no missing migrations. All external
services are faked and tests use in-memory SQLite.

## Conservative choices and limits

Task updates require explicit YES as well as reschedules/cancellations; the
brief specifies automatic execution only for creates and task completions.
Attachments have a combined 10 MB allowance per incoming message, limiting
request size when several files arrive. Native creation continues to use the
phase 2 synthetic `agent-proposal:<id>` source record and existing pipeline.
An external send accepted immediately before a process crash remains an
uncertain-delivery window; sent ids and recorded replies reduce retries but
cannot provide exactly-once delivery. No live account tests were run.
