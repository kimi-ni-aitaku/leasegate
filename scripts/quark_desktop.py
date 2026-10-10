"""python -m scripts.quark_desktop; never prints credentials or clicks agreements."""
import argparse
import json
from fastapi import HTTPException
from app.config import Settings, load_settings
from app.security import Vault
from app.providers.quark_cookie import CookieStore
from app.providers.quark_desktop import login_local_desktop


def main():
    parser = argparse.ArgumentParser(description='Official web-to-desktop diagnostic on this computer only')
    parser.add_argument('--env-file')
    parser.add_argument('--cookie-file')
    args = parser.parse_args()
    try:
        settings = Settings(_env_file=args.env_file) if args.env_file else load_settings()
        store = CookieStore(args.cookie_file or settings.quark_cookie_file, Vault(settings.encryption_key))
        result = login_local_desktop(store.load()['cookie'])
        print(json.dumps(result, ensure_ascii=False))
    except HTTPException as exc:
        print(json.dumps({'error': exc.detail}))
        raise SystemExit(1) from None
    except Exception:
        print(json.dumps({'error': 'desktop_diagnostic_failed; sensitive details suppressed'}))
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
