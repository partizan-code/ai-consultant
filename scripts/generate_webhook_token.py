"""Print a new webhook secret for manual transfer to .env; never edit files."""

import secrets


def main() -> None:
    print(f"WEBHOOK_TOKEN={secrets.token_urlsafe(32)}")


if __name__ == "__main__":
    main()
