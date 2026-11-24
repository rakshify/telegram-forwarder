"""Platform-neutral pieces shared by every source and destination.

Modules
-------
rich      text plus formatting spans (bold, quote, ...) and the helpers to
          join, trim and split it
model     what sources produce (Message) and where it goes (Pair, Dest)
store     the on-disk queue, the message map and sender names (SQLite)
source    the interface a source implements, and what the pipeline offers it
sink      the interface each destination implements, and the errors it reports
pipeline  queued messages -> every pair's destination: fan-out, replies,
          edits, retries, alerts

Nothing here imports a platform library.
"""
