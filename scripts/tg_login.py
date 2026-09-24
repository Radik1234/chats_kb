#!/usr/bin/env python3
"""One-off interactive login to obtain a Telethon StringSession.

Run once on a trusted machine to bootstrap the backfill worker's user session:

    pip install "Telethon>=1.40,<2"
    TG_API_ID=... TG_API_HASH=... python scripts/tg_login.py

It asks for your phone, the login code (and 2FA password if enabled), then
prints a StringSession. Put that value into TG_SESSION as a secret (never commit
it). api_id/api_hash come from https://my.telegram.org.
"""

import os
import sys


def main() -> int:
    try:
        from telethon.sync import TelegramClient
        from telethon.sessions import StringSession
    except ImportError:
        print('Telethon is not installed. Run: pip install "Telethon>=1.40,<2"', file=sys.stderr)
        return 2

    api_id = os.environ.get("TG_API_ID")
    api_hash = os.environ.get("TG_API_HASH")
    if not (api_id and api_hash):
        print("Set TG_API_ID and TG_API_HASH (from https://my.telegram.org).", file=sys.stderr)
        return 2

    with TelegramClient(StringSession(), int(api_id), api_hash) as client:
        session = client.session.save()
        me = client.get_me()
        print(f"\nLogged in as: {getattr(me, 'username', None) or me.first_name} (id={me.id})")
        print("\n==== TG_SESSION (keep secret, do NOT commit) ====")
        print(session)
        print("==================================================")
    return 0


if __name__ == "__main__":
    sys.exit(main())
