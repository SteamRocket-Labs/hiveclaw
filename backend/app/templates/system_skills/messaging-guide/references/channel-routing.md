# Channel Routing Reference

Use the narrowest configured channel that matches the recipient and delivery
requirements.

## Recipient Routing

| Recipient | Preferred path |
| --- | --- |
| Current human requester | Normal reply or `send_channel_message` |
| Web-platform user | `send_web_message` |
| Feishu human user | Feishu Integration then `send_feishu_message` |
| Another digital employee | Delegation Guide tools |
| External stakeholder | Email Guide |

## File and Image Delivery

- Use `send_channel_file` for workspace files to the current requester.
- Use `upload_image` when a durable image URL is needed.
- Confirm the file exists before attempting delivery.

## Safety

Do not silently switch channels when the requested channel is unavailable.
Report the missing configuration or ask for a reachable recipient.
