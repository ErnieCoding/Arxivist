#!/usr/bin/env python3
"""
API-key management CLI for the Arxivist backend module.

Usage (on the host):
    python scripts/apikeys.py create --label "recruiting-platform"
    python scripts/apikeys.py list
    python scripts/apikeys.py revoke <key_id>

Or inside the container (config/ is the same bind-mounted volume):
    docker exec arxivist python scripts/apikeys.py create --label "partner-x"

The full key is printed ONCE at creation and never stored — only its sha256
hash lives in config/api_keys.json. Revocation takes effect immediately (the
app re-reads the store per request; no restart needed).
"""

import argparse
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import api_keys


def _fmt_ts(ts) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def cmd_create(args):
    result = api_keys.create_key(args.label)
    print("API key created. SAVE IT NOW — it will not be shown again.\n")
    print(f"  key_id : {result['key_id']}")
    print(f"  label  : {result['label']}")
    print(f"  key    : {result['key']}")
    print("\nPass it in requests as:  X-API-Key: <key>")


def cmd_list(_args):
    keys = api_keys.list_keys()
    if not keys:
        print("No API keys. Create one with: python scripts/apikeys.py create --label <name>")
        return
    print(f"{'key_id':<10} {'label':<26} {'created':<18} {'last used':<18} status")
    print("-" * 86)
    for rec in keys:
        status = "REVOKED" if rec.get("revoked") else "active"
        print(f"{rec['key_id']:<10} {rec.get('label','')[:24]:<26} "
              f"{_fmt_ts(rec.get('created_at')):<18} {_fmt_ts(rec.get('last_used_at')):<18} {status}")


def cmd_revoke(args):
    if api_keys.revoke_key(args.key_id):
        print(f"Key {args.key_id} revoked. Takes effect immediately.")
    else:
        print(f"No active key with id {args.key_id}.")
        sys.exit(1)


def main():
    p = argparse.ArgumentParser(description="Manage Arxivist API keys")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("create", help="mint a new key (printed once)")
    c.add_argument("--label", required=True, help="who/what this key is for")
    c.set_defaults(fn=cmd_create)

    lst = sub.add_parser("list", help="list keys (hashes never shown)")
    lst.set_defaults(fn=cmd_list)

    r = sub.add_parser("revoke", help="revoke a key by key_id")
    r.add_argument("key_id")
    r.set_defaults(fn=cmd_revoke)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
