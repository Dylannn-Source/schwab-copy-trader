"""One-off, read-only: prints the exact input schema for get_option_orders /
get_equity_orders, and lists any existing orders on the account, so fill
verification can be built against real field names (order_id, state values)
instead of guessed ones.

Usage:
    python3 inspect_order_schema.py
"""
import json

from robinhood_mcp_client import RobinhoodMCPClient

TOOLS_OF_INTEREST = ["get_option_orders", "get_equity_orders"]


def main():
    client = RobinhoodMCPClient()
    client.start()

    result = client.list_tools()
    for tool in result.tools:
        if tool.name in TOOLS_OF_INTEREST:
            print(f"=== {tool.name} input schema ===")
            print(json.dumps(tool.input_schema, indent=2))
            print()

    for name in TOOLS_OF_INTEREST:
        r = client.call_tool(name, {"account_number": "604444141"})
        print(f"=== {name} sample response ===")
        print(json.dumps(r.structured_content, indent=2)[:4000])
        print()

    client.stop()


if __name__ == "__main__":
    main()
