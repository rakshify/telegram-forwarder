"""WhatsApp as a source: WhatsApp chats -> Telegram and Signal chats.

A WhatsApp account is linked as a companion device (like WhatsApp Web), and
the messages of chosen chats go through the pipeline (src/core/pipeline.py)
to Telegram, posted by the same bot the Telegram forwarder uses, or to
Signal through signal-cli-rest-api.

Modules
-------
client       the only module that imports neonize (WhatsApp client library)
accounts     wa_accounts.json registry, short ids, per-account file paths
config_file  configs/wa-<short_id>.json loader
formatting   WhatsApp markup -> Rich text (src/core/rich.py)
convert      WhatsApp message -> what to post (pure, unit-tested)
source       the source the pipeline runs: connection, session events,
             queuing, decoding, media downloads
bridge       `wa-forward`: puts the source, the sinks and the pipeline together
link         link / unlink / list accounts and groups (CLI commands)
"""
