"""
Run this once to authenticate and discover your account hashes.
After running, copy the hashes into config.json.
"""
import json
import schwab


def main():
    with open("config.json") as f:
        config = json.load(f)

    print("Opening browser for Schwab OAuth login...")
    client = schwab.auth.client_from_login_flow(
        config["app_key"],
        config["app_secret"],
        config["redirect_uri"],
        config["token_path"],
    )

    resp = client.get_account_numbers()
    resp.raise_for_status()
    accounts = resp.json()

    print("\nFound accounts:")
    for i, acct in enumerate(accounts):
        print(f"  [{i}] Number: {acct['accountNumber']}  Hash: {acct['hashValue']}")

    print("\nCopy the hash values into config.json:")
    print('  "leader_account_hash": "<hash of leader account>"')
    print('  "follower_account_hashes": ["<hash of follower account>", ...]')


if __name__ == "__main__":
    main()
