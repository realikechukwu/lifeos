## Summary

Stacked on `main` (phase 1). Target: `main`. Do not merge or deploy automatically.

Replace the agent's OpenAI handoff with validated native proposals for creates,
task completion/editing, and one-off event/reminder management. Replies precede
HTML-safe Confirm/Cancel cards. Decisions are requester-scoped, expire at 24h,
and claim the proposal before executing to prevent duplicate taps. Corrections
supersede old cards. Actual outcomes enter the next user/system context pair;
stored API history remains append-only and the cached prompt/tools stay static.
Calendar reads expose LifeOS-owned ids. The admin execution path now retains
linked reminders. Legacy extraction and inbox routing remain available.

## Settings, migrations, dependencies

- `AGENT_PROPOSALS_ENABLED=false` by default; enable for native changes.
- `0013_agent_proposals`: AgentProposal and AgentOutcome.
- No new dependencies; Anthropic remains exactly 1.14.0.
- Two existing time-sensitive task tests now use relative future dates.

## Exact server steps (owner only; none run by this PR)

After the normal reviewed-code deployment installs requirements and applies
migration 0013, add `AGENT_PROPOSALS_ENABLED=true` to the shared server environment
file. Retain `AGENT_ENABLED=true` and `ANTHROPIC_API_KEY`. Run only:

```bash
sudo systemctl restart lifeassistant-web lifeassistant-agent
```

No new service, timer or webhook registration is needed.

## Manual checklist

- [ ] Ask for two tasks: short reply, then two cards; confirm one, cancel the other.
- [ ] Tap Confirm twice; only one task exists. Wife cannot decide Ike's group card.
- [ ] Correct a pending event time; old card shows replaced and cannot execute.
- [ ] Create an event with a day-before reminder; both appear once.
- [ ] Look up that event; reschedule then cancel it; recurring/foreign events reject edits.
- [ ] Ask what happened; the agent sees actual outcomes in the following turn.
- [ ] Try an expired card and a past date; neither writes household data.
- [ ] Disable AGENT_ENABLED; legacy typed/voice extraction and pending inbox edits work.

## Validation

`DATABASE_URL= .venv/bin/python manage.py test --noinput`: 254 tests pass.
Google, Anthropic and Telegram are faked; tests use in-memory SQLite.

## Operational limits

An external service can accept a write just before a process crash. A claimed
proposal is not retried automatically, even if its result is uncertain. Card
sending has the same small delivery/recording crash window as other bot replies.
Task editing uses the existing service's supported fields and clears due_time;
this is stated in the tool description. Recurring series are read-only.
