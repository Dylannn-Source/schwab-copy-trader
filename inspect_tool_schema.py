"""One-off: prints the exact input schema for a few Robinhood MCP tools so we
can wire up order placement against real field names instead of guessing.

Usage:
    python3 inspect_tool_schema.py
"""
import json

from robinhood_mcp_client import RobinhoodMCPClient

TOOLS_OF_INTEREST = [
    "place_option_order",
    "place_equity_order",
    "get_accounts",
    "get_option_chains",
    "get_option_instruments",
]


def main():
    client = RobinhoodMCPClient()
    client.start()
    result = client.list_tools()
    for tool in result.tools:
        if tool.name in TOOLS_OF_INTEREST:
            print(f"=== {tool.name} ===")
            print(json.dumps(tool.input_schema, indent=2))
            print()
    client.stop()


if __name__ == "__main__":
    main()
