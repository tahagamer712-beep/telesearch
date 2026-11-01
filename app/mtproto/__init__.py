"""MTProto large-file bridge.

Bot API getFile refuses files over 20MB on the hosted API, so documents
larger than the bot's download limit that arrive FROM an authorized
account are not rejected: they are recorded as pending (pending.py)
and a separate bridge process (bridge.py, Telethon, running as the
owner's account) downloads them into data/incoming, where the existing
watcher ingests them. Nothing in this package sends Telegram messages.
"""
