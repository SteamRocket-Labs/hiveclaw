# Email Delivery Policy

Email is for formal external delivery, inbox-backed communication, and
artifact sharing that needs a durable thread.

## Use Email When

- The recipient is outside the organization.
- The message needs attachments or a formal record.
- The user asks to read or reply to an inbox thread.
- A channel-specific chat tool is unavailable for the target recipient.

## Avoid Email When

- The recipient is a digital employee; use delegation or agent messaging.
- The recipient is available in a configured real-time channel and the request is operational.
- The email tool reports missing credentials or SMTP/IMAP configuration.

## Boundary

Email credentials are managed by tool settings. Never search environment
variables or workspace files for SMTP passwords, IMAP passwords, OAuth tokens,
or mailbox secrets.
