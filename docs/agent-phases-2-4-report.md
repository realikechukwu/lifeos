# LifeOS agent phases 2–4: implementation and review report

## Delivery

The work is split into three branches and PRs, all targeting `main`:

| Phase | Branch | Stacked on | Review |
| --- | --- | --- | --- |
| 2 | `agent-phase-2` | Phase 1 on `main` | [PR #2](https://github.com/realikechukwu/lifeos/pull/2) |
| 3 | `agent-phase-3` | `agent-phase-2` | [PR #3](https://github.com/realikechukwu/lifeos/pull/3) |
| 4 | `agent-phase-4` | `agent-phase-3` | The PR containing this report |

No branch was merged, nothing was pushed to `main`, and no production server
was accessed. Local Django commands explicitly cleared `DATABASE_URL`.
The original, owner-supplied `docs/codex-agent-phases-2-4.md` remains untouched.

## What was built

### Phase 2: native proposals

`AgentProposal` records all nine requested operations, their validated payload,
requester, thread, card identity, replacement chain, decision and real result.
`AgentOutcome` retains undelivered changes for the next user/system context pair.
Native `propose_*` tools replace `request_change`; the agent never calls the
legacy OpenAI extractor. Legacy conversations, inbox edits and the original
non-agent Telegram route remain available.

Python validates dates, future times, event ordering, recurrence combinations,
closed recipient/assignee choices, existing editable records, and ownership of
one-off calendar records. It checks again at execution. A correction supersedes
only that requester's unexpired pending proposal in the same thread and updates
the old card. Replies precede HTML-escaped cards. Callback payloads are small
`ag:<id>:ok/no` strings, scoped to the requester and chat. Expiry is enforced at
decision time; expired proposals are omitted from the next turn's pending list.

A conditional database update claims a pending decision before executing it.
Creates enter the existing ParsedAction/admin service pipeline through synthetic
`agent-proposal:<id>` source records. Other operations call the existing task,
Telegram inbox and Google Calendar services. The admin event path now creates
linked reminders through the router's existing helper; duplicates do not create
another event or linked reminder. Calendar reads expose LifeOS ids, including
ids in the local fallback view.

### Phase 3: agent email channel

The poller saves incoming email and retains unauthorised handling. With the
email flag on, authorised adult senders get a unique AgentJob per IncomingEmail.
Self-sent, automated and bulk/list/junk mail cannot start an agent loop. Other
authorised household roles continue through the original extractor.

One Gmail thread maps to one AgentThread without idle rollover. Each email is
its own job and gets its own reply/label. Sender-written instructions are
separated from quoted/forwarded data. Subject and received/forwarded dates enter
the context note. PDF/JPEG/PNG attachments are native API blocks within a combined
10 MB allowance; other attachments are named. Incoming RFC Message-ID,
Auto-Submitted and Precedence headers are stored.

Clear creates and task completions auto-confirm through the proposal executor.
Other mutations wait for a later YES. `confirm_proposal` checks the newest
sender-written text, same role/thread, and evidence that the proposal was offered
in an earlier email. A bare YES is accepted only for a single pending proposal;
`YES <id>` selects among several. Quoted confirmations cannot authorise writes.

Replies go only to the real outer sender, with CC resolved by the existing
household-only rule. They carry Gmail thread, In-Reply-To and References headers,
include server-recorded outcomes, and end with LifeOS. Replies are persisted
before sending so a delivery retry reuses the body without rerunning tools.
Success applies Processed; permanent worker failure applies Failed and attempts
a short apology to an authorised human. Transient Google API status errors join
the existing bounded worker retry policy.

### Phase 4: drafts, profiles, documents and web reading

Private Telegram PDF/DOCX uploads download through the existing bounded file
client, extract source text server-side, and queue captions with native PDF
blocks or DOCX text. Email PDFs/DOCX can enter the same profile workflow.
`AgentUpload` retains extracted text; `ProfileDocument` stores the requesting
role, title/kind, exact extracted text, source filename and active status.
Save/list/read tools cannot access the other person's documents. Saving never
accepts model-supplied replacement text.

`make_document` renders headings, bullets, numbering and bold to DOCX. Private
GeneratedDocument rows retain the binary and content; delivery records send the
file after Telegram text or attach it to the Gmail reply. Listing and resending
generated documents work across later conversations for the same person.
Telegram has a bounded multipart `sendDocument` client. Gmail fresh messages and
replies support MIME attachments.

`email_me` resolves only the speaker's configured authorised address and rejects
arbitrary recipient fields. Database-unique daily slots enforce ten sends per
person per local day, including failed or uncertain attempts. An owned generated
document can be attached.

The loop uses `client.beta.messages.stream(...).get_final_message()` with
32,000 output tokens. Model, adaptive thinking, effort, fallback beta, fallbacks,
refusal handling and explicit 1h cache controls remain intact. Every returned
block is stored using `to_dict(mode="json")`, including thinking, server search
results and error blocks; pause_turn continues without rewriting history.
The registered web tools use exactly the requested 20260209 versions and five
uses per API call. No code_execution tool is declared.

The static system prompt covers factual CV tailoring from saved sources,
untrusted adverts/pages/attachments, recipient restrictions and drafting.
Read-only admin views expose proposals, uploads and generated-document metadata;
profile active status can be changed with an audit entry. Binary data is excluded
from admin forms. Telegram batch retries now retain the original batch, and a
failed input-only turn is closed with an appended assistant marker before a new
user/system pair, preserving message ordering without editing history. A recovered
interrupted write reports uncertainty and disables further tools for that retry
turn instead of claiming the write never ran.

## Design decisions and deviations from the brief

1. **Web tools and the default-off flag conflict.** Server tools execute inside
   Anthropic, so a Python handler cannot reject their execution when disabled.
   The allowed tool_choice values cannot selectively disable them while keeping
   household tools working. Therefore `AGENT_WEB_ENABLED` selects a fixed tool
   list once at worker startup: disabled workers omit the two remote tools.
   Every request within that worker uses the same sorted list, across channels
   and roles. Changing the flag requires restart and changes the cached prefix.
   This is the deliberate exception to the brief's stronger requirement for a
   flag-independent tool set. All local tools remain defined regardless of flag
   and return errors when disabled. This prioritises an effective default-off
   switch over relying on a model instruction to avoid remote web calls.
2. **Email task updates also require YES.** Automatic execution was explicitly
   specified for creates and task completions. Requiring confirmation for the
   remaining mutations is the conservative interpretation.
3. **Confirmation wording is deliberately narrow:** YES or YES <id>, ignoring
   case and a final full stop/exclamation mark. Broader phrases such as “yes,
   but change the time” require another clarification rather than accidentally
   authorising an unchanged proposal.
4. **Attachment limits are combined per email.** Individual Telegram uploads
   remain capped at 10 MB. Email attachments share 10 MB so several files cannot
   silently create an enormous API request. Extraction additionally caps PDFs
   at 200 pages, DOCX expanded data at 25 MB/1,000 ZIP entries, and extracted text
   at 200,000 characters. Unreadable/scanned sources cannot be saved as profiles.
5. **Generated binaries use private database storage.** This avoids public media
   URLs, extra server paths/permissions and deployment configuration. It trades
   operational simplicity for increased database/backup size.
6. **Synthetic create sources are retained across channels.** Email-created
   proposals reuse phase 2's mandated synthetic TELEGRAM IncomingEmail execution
   records. The email job retains the actual IncomingEmail and the proposal is
   linked to its email thread; downstream source labels on created records can
   therefore say Telegram. A future source-kind migration could make this
   provenance clearer without changing execution logic.
7. **With native proposals disabled, the enabled agent is read-only.** The old
   request_change handoff was removed as requested. Disabling AGENT_ENABLED
   still restores the original Telegram extraction behavior.

## Validation

| Checkpoint | Full Django suite |
| --- | --- |
| Baseline | 245 tests; the two documented fixed-date task failures reproduced |
| Phase 2 | 254 passed |
| Phase 3 | 270 passed |
| Phase 4 | 294 passed |

The two existing task tests now use relative future dates. The final command was
`DATABASE_URL= .venv/bin/python manage.py test --noinput`. Tests use in-memory
SQLite and fake Anthropic, Google, Gmail and Telegram services. New tests cover
proposal validation/execution/cards/ownership/expiry/replacement/outcomes,
email routing/continuity/attachments/confirmation/recipients/labels/retries,
PDF/DOCX extraction and profile isolation, DOCX round-trip opening, MIME and
multipart transports, daily email limits, streaming, cache equality, server-tool
errors and exact history replay. No live integration tests were run.

`makemigrations --check --dry-run` reports no missing migrations; `pip check`
reports no broken requirements; `git diff --check` passes. New direct/transitive
packages are exactly pinned: pypdf 6.20.1, python-docx 1.2.0, lxml 6.1.3 and
typing_extensions 4.16.0. Anthropic remains 1.14.0.

## Known gaps and risks

- External effects cannot be guaranteed exactly once across a process crash.
  Confirmed means the proposal was claimed; its result may still say execution
  started if the process died. Inspect the result, AuditLog and provider before
  making a replacement request. Uncertain proposal execution is never retried
  automatically. Bot/Gmail delivery has a similar accept-before-record window.
- Tests do not validate real account permissions, provider availability, live
  SDK/model behavior, Linux installation or user-visible Word rendering in a
  desktop client. PR checklists are the remaining owner-run integration work.
- CV factual grounding is enforced through source retention, role checks and
  model guidance. There is no deterministic claim-by-claim verifier. Generated
  applications should be reviewed before use; tools do not send to employers.
- No OCR is included. JPEG/PNG are model-readable but cannot be saved as profile
  text through this server-side extraction workflow. Original DOCX binary uploads
  are not retained; extracted text is. Native PDF/image blocks remain in stored
  API history as required by append-only replay.
- No retention/erasure policy is added for histories, extracted profiles,
  generated binaries, outcome rows or quota reservations. Storage and backups
  will grow. Deactivation stops profile tool reads but is not erasure.
- Task editing supports the existing service's title/date/assignee fields and
  clears due_time. Recurring series remain read-only. Event ownership and
  recurrence checks use LifeOS's local records; manual provider-side changes
  can make that mirror stale.
- PDF/DOCX parsing has input bounds but remains synchronous during upload/poll
  processing. Unusually complex files can still take time; there is no separate
  sandboxed parser service in this scope.

## Open questions for the owner

- Is the documented startup-only web flag/tool-list exception acceptable?
- Should email task updates remain confirmation-only, and should broader YES
  wording be supported once explicit confirmation semantics are agreed?
- What retention/deletion periods should apply to profile text, attachments in
  histories, generated documents and operational records?
- Should created-record provenance gain an explicit agent/email source kind?
- Should a later phase support OCR, richer CV templates, due-time editing or
  provider reconciliation before modifying manually changed calendar records?

Each PR description includes its flags, migrations, dependencies, exact server
environment/restart steps, manual checklist and test results. README,
`.env.example` and DEPLOY_ORACLE.md reflect the final behavior. Deployment and
live manual checks remain owner actions after review.
