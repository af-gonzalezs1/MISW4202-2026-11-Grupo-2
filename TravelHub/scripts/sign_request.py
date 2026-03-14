#!/usr/bin/env python3
import argparse
import hashlib
import hmac


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Genera firma HMAC-SHA256 para requests anti-tampering."
    )
    parser.add_argument("--method", required=True, help="HTTP method, e.g. PATCH")
    parser.add_argument("--path", required=True, help="Path exacto, e.g. /admin/reservas/<id>/estado")
    parser.add_argument("--timestamp", required=True, help="ISO timestamp en UTC, e.g. 2026-03-06T18:00:00Z")
    parser.add_argument("--body", required=True, help='Body JSON exacto, e.g. {"estado":"PAGADA"}')
    parser.add_argument("--key", required=True, help="Request signing key")
    args = parser.parse_args()

    canonical = f"{args.method.upper()}\n{args.path}\n{args.timestamp}\n{args.body}"
    signature = hmac.new(
        args.key.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    print(signature)


if __name__ == "__main__":
    main()
