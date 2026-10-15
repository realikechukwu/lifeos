## Summary

Stacked on `agent-phase-3` (PR #3), which includes phase 2 (PR #2).
This PR targets `main` as requested. Review this phase's additional commit.
Do not merge or deploy automatically.

Add private PDF/DOCX uploads, server-saved profile documents, factual CV/application
guidance, generated Word documents and resend tools. Telegram uses multipart file
uploads; Gmail attaches generated files to the reply. email_me locks recipients
to the speaker's own configured address and reserves one of ten daily DB slots.
Generated files are private database records, with no public media setup.

Switch Claude to streaming with 32,000 output tokens while retaining the exact
model/thinking/effort/fallback/cache parameters and verbatim block history. Add
20260209 server web_search/web_fetch tools, max_uses 5, without code_execution.
Preserve batched Telegram inputs on retries and close failed input-only turns
with append-only assistant markers before the next user/system pair.

## Flags, migration and pinned dependencies

- `AGENT_DOCUMENTS_ENABLED=false`: uploads, profiles, DOCX and email_me.
- `AGENT_WEB_ENABLED=false`: read at process startup, see deviation below.
- Migration `0015_agent_documents`: AgentUpload, ProfileDocument,
  GeneratedDocument, AgentDocumentDelivery and AgentEmailDelivery.
- New exact pins: `pypdf==6.20.1`, `python-docx==1.2.0`, `lxml==6.1.3`,
  `typing_extensions==4.16.0`. Anthropic stays exactly 1.14.0.

## Exact server steps (owner only; nothing run here)

After the normal reviewed-code deployment installs requirements and applies
migration 0015, add to the shared environment file:

```dotenv
AGENT_DOCUMENTS_ENABLED=true
AGENT_WEB_ENABLED=true
```

Retain `AGENT_ENABLED=true` for Telegram, `AGENT_EMAIL_ENABLED=true` for email,
`AGENT_PROPOSALS_ENABLED=true` for household writes, the Anthropic key, and the
configured household email addresses. Run:

```bash
sudo systemctl restart lifeassistant-web lifeassistant-agent
```

No new service, timer, media directory or reverse-proxy configuration is needed.
The existing Gmail oneshot reads the new environment on its next run.

## Manual checklist

- [ ] Upload a PDF CV privately in Telegram with a save caption; list/read it.
- [ ] Repeat with DOCX, and attach each format to an agent email.
- [ ] Verify the other adult cannot list/read your profiles or generated files.
- [ ] Confirm oversized, unreadable/encrypted files and group document uploads are rejected/ignored.
- [ ] Read a public job advert; tailor using saved facts without invented experience.
- [ ] Generate a DOCX containing headings, bullets and bold; open it in Word/LibreOffice.
- [ ] Verify Telegram reply precedes the document, and email receives a MIME attachment in the same thread.
- [ ] Start a new conversation and resend an existing generated document.
- [ ] Ask email_me for a draft with a document; it reaches only your own configured address.
- [ ] Verify the eleventh email_me send is rejected; the other adult has a separate quota.
- [ ] Disable native flags; handlers reject calls. Disable web and restart; remote tools are omitted.
- [ ] Verify long replies complete through streaming and history/cache usage remain visible.
- [ ] Inspect proposal outcomes and profile deactivation in Django admin.

## Validation

`DATABASE_URL= .venv/bin/python manage.py test --noinput`: all 294 tests pass.
All external APIs are faked; SQLite is in-memory. Tests open generated documents
with python-docx and inspect Gmail MIME/Telegram multipart requests. Web result
errors and pause_turn preserve thinking/server blocks verbatim. Migrations have
no drift, `pip check` passes, and `git diff --check` passes.

## Required deviation: effective web flag vs identical tool set

Remote server tools cannot be blocked by a local ToolInputError handler, and
permitted tool_choice values cannot selectively disable them. Therefore the
web flag selects the tool list once at worker startup. Disabled workers omit
the two remote tools; every call within a worker uses the same sorted set,
across roles/channels. A flag change requires restart and changes the cached
prefix. This deliberately relaxes the brief's flag-independent-list invariant
to provide an effective default-off web capability. All native tools remain
present and have runtime flag checks.

Other conservative choices: email task updates require YES; attachments share
10 MB per email; extraction has page/expanded-size/text limits; no OCR or public
file URLs. External accept-before-record crashes remain uncertain rather than
exactly-once. Profile retention and provider-side record reconciliation remain
future work. The detailed reviewer report is
`docs/agent-phases-2-4-report.md`, committed in this branch.
