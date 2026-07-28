"""Fetch and print the full raw order object for a given order ID.

Run this next to your existing config.json / schwab_token.json (same
directory the app uses) to see exactly what Schwab returned for an order,
including fields the dashboard's activity log doesn't surface.

Usage:
    python diagnose_order.py <order_id> [account_hash]

If account_hash is omitted, the first entry in follower_account_hashes from
config.json is used.
"""
import json
import os
import sys
from pathlib import Path

import schwab

CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", "config.json"))


def main():
    if len(sys.argv) < 2:
        print(f"Usage: python {sys.argv[0]} <order_id> [account_hash]")
        sys.exit(1)
    order_id = sys.argv[1]

    with open(CONFIG_PATH) as f:
        config = json.load(f)

    account_hash = sys.argv[2] if len(sys.argv) > 2 else config["follower_account_hashes"][0]
    token_path = config.get("token_path", "schwab_token.json")

    client = schwab.auth.client_from_token_file(
        token_path, config["app_key"], config["app_secret"],
    )

    resp = client.get_order(order_id, account_hash)
    print(f"HTTP {resp.status_code}")
    print(json.dumps(resp.json(), indent=2, default=str))


if __name__ == "__main__":
    main()
