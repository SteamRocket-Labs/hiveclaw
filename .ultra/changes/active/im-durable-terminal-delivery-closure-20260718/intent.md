# IM user-proven identity and Agent Detail QR closure

- Change: `im-durable-terminal-delivery-closure-20260718`
- Kind: `incident`
- Base commit: `ad4a9e4bcb8cf19977bbd6349e48035bafdaa548`
- Documentation impact: `required`

## Intent

Correct the IM identity authority so users self-bind through provider proof while administrators may only unlink or revoke, and repair the production Agent Detail Feishu/Lark QR generation failure. Preserve durable transport ACK isolation and terminal delivery.

## Documentation rationale

Record the corrected identity authority and production acceptance evidence.

