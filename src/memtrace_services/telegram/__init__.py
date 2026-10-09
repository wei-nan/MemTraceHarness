"""The Telegram service: one bot, one Harness.

It owns the bot token and the chat allowlist, long-polls Telegram for what people say, hands it to
its Harness over the Harness API, and delivers what the Harness wants said (the outbox) back as
Telegram messages. The Harness itself knows nothing about Telegram.
"""
