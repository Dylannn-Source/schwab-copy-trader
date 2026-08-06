"""One-off, read-only: looks up AAPL's option chain and one contract's
instrument data, to confirm the response shape before wiring up order
placement.

Usage:
    python3 inspect_option_chain.py
"""
import json

from robinhood_mcp_client import RobinhoodMCPClient


def main():
    client = RobinhoodMCPClient()
    client.start()

    chains = client.call_tool("get_option_chains", {"underlying_symbol": "AAPL"})
    print("=== get_option_chains ===")
    print(json.dumps(chains.structured_content, indent=2)[:3000])

    data = chains.structured_content.get("data", {})
    chain_list = data.get("chains") or data.get("results") or []
    if chain_list:
        chain_id = chain_list[0].get("id") or chain_list[0].get("chain_id")
        expirations = chain_list[0].get("expiration_dates", [])
        print(f"\nUsing chain_id={chain_id}, first expiration={expirations[:1]}")

        if chain_id and expirations:
            instruments = client.call_tool("get_option_instruments", {
                "chain_id": chain_id,
                "expiration_dates": expirations[0],
                "type": "call",
            })
            print("\n=== get_option_instruments ===")
            print(json.dumps(instruments.structured_content, indent=2)[:3000])

    client.stop()


if __name__ == "__main__":
    main()
