"""Generate secrets once. Never prints the generated values."""
import argparse
import base64
import os
from pathlib import Path
import secrets

parser = argparse.ArgumentParser()
parser.add_argument('--path', default='.env')
args = parser.parse_args()
path = Path(args.path)
if not path.exists():
    path.parent.mkdir(parents=True, exist_ok=True)
    values = {'SIGNING_KEY': secrets.token_urlsafe(48), 'ENCRYPTION_KEY': base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
              'WEBHOOK_SECRET': secrets.token_urlsafe(48), 'ADMIN_TOKEN': secrets.token_urlsafe(48)}
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as output:
        output.write(''.join(f'{key}={value}\n' for key, value in values.items()))
    print(f'Created secret configuration at {path}; values suppressed')
else:
    print(f'Using existing secret configuration at {path}')
