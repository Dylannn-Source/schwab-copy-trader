"""One-off, read-only: calls get_accounts and prints the raw result shape so
we can write correct parsing code for account_number, agentic_allowed, and
option_level before wiring up real order placement.

Usage:
    python3 inspect_accounts.py
"""
import json

from robinhood_mcp_client import RobinhoodMCPClient


def main():
    client = RobinhoodMCPClient()
    client.start()
    result = client.call_tool("get_accounts", {})
    print("=== structuredContent ===")
    print(json.dumps(result.structuredContent, indent=2) if result.structuredContent else "(none)")
    print("\n=== content ===")
    for block in result.content:
        print(block)
    client.stop()


if __name__ == "__main__":
    main()
