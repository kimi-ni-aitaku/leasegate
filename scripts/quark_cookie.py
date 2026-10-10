"""Run with python -m scripts.quark_cookie. Input is hidden; output contains no credentials."""
import argparse
import getpass
import json
from fastapi import HTTPException
from app.config import load_settings
from app.security import Vault
from app.providers.quark_cookie import CookieStore, parse_cookie, check_account


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['import', 'check', 'status', 'clear'])
    args = parser.parse_args()
    settings = load_settings()
    store = CookieStore(settings.quark_cookie_file, Vault(settings.encryption_key))
    try:
        if args.action == 'import':
            store.save(parse_cookie(getpass.getpass('Raw Cookie header (hidden): ')))
        if args.action == 'clear':
            store.clear()
        result = check_account(store.load()) if args.action == 'check' else store.status()
        print(json.dumps(result, ensure_ascii=False))
    except HTTPException as exc:
        print(json.dumps({'error': exc.detail}))
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
