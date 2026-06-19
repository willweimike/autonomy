---
name: email-himalaya
description: Use Himalaya CLI for governed terminal email workflows.
version: 1.0.0
tags: [email, himalaya, imap, smtp, cli, communication]
platforms: [macos, linux, windows]
requires_tools: [shell.execute]
---

# Email with Himalaya

Use this procedure when the task asks to list, search, read, compose, reply to,
forward, move, delete, or download email through the external Himalaya CLI.
This Procedure Skill is guidance only; ActionGateway approval and tool
boundaries still decide execution.

Workflow:
- Verify the CLI first with `himalaya --version`.
- Verify configuration exists by checking `~/Library/Application Support/himalaya/config.toml`, but
  Do not read secret-bearing config values.
- Prefer read-only commands before write operations: list accounts, folders,
  envelopes, then read the specific message.
- Use `--output json` when listing or searching if structured output helps the
  next step.
- Use explicit account and folder flags when the user names an account or
  folder; otherwise use Himalaya defaults.
- For non-interactive sending, pipe a complete message or template into
  `himalaya template send` instead of opening `$EDITOR`.

Common commands:
- `himalaya account list`
- `himalaya folder list`
- `himalaya envelope list --output json`
- `himalaya envelope list --folder "INBOX" --page 1 --page-size 20 --output json`
- `himalaya message read MESSAGE_ID`
- `himalaya message export MESSAGE_ID --full`
- `himalaya message move MESSAGE_ID "Archive"`
- `himalaya message delete MESSAGE_ID`
- `himalaya attachment download MESSAGE_ID --dir ./downloads`

Sending pattern:
```bash
cat message.txt | himalaya template send
```

Tool use rules:
- Email sends, deletes, moves, and flag changes have external side effects and
  may require approval.
- Do not invent recipients, subjects, message IDs, folders, or account names.
- Do not expose passwords, app passwords, OAuth tokens, or keychain command
  output.
- Do not retry a failed send automatically. First inspect whether SMTP delivery
  may have succeeded and only Sent-folder saving failed.
- If Gmail is used, check that the user configured `folder.aliases.sent` as
  `[Gmail]/Sent Mail`; the old singular `folder.alias` form can cause a send to
  deliver successfully, fail while saving to Sent, and duplicate mail on retry.
- Keep command output bounded and summarize only the message fields needed for
  the task.

Pitfalls:
- Himalaya message IDs are folder-relative; re-list after changing folders.
- Interactive compose opens `$EDITOR`; avoid it unless the user explicitly asks
  for an editor-driven flow.
- A non-zero send exit code is not proof the recipient did not receive mail.
- Attachment downloads write files; choose a workspace path unless the user
  explicitly asks for another destination.

Outcome checks:
- Read/list/search observations should include account or folder context,
  message IDs, sender, subject, date, and a short relevant excerpt.
- Send observations should include recipient, subject, command exit status, and
  any warning about Sent-folder persistence.
- Continue if the mailbox state, message ID, or delivery status is ambiguous.
