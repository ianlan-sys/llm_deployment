"""Render .env from .env.example with freshly generated demo credentials.

Runs inside the backend image so the host needs no Python. The rendered file
goes to stdout; the one-time plaintext password goes to stderr only.
"""
import base64
import hashlib
import re
import secrets
import string
import sys

template_path, username = sys.argv[1], sys.argv[2]
if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", username):
    sys.exit("invalid username")

alphabet = string.ascii_letters + string.digits
password = "".join(secrets.choice(alphabet) for _ in range(20))
salt = secrets.token_bytes(16)
digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
values = {
    "DEMO_AUTH_REQUIRED": "true",
    "DEMO_AUTH_USERNAME": f"'{username}'",
    "DEMO_AUTH_PASSWORD_HASH": f"'scrypt$16384$8$1${salt.hex()}${digest.hex()}'",
    "DEMO_SESSION_SECRET": "'"
    + base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")
    + "'",
}

with open(template_path, encoding="utf-8") as template:
    for line in template:
        match = re.match(r"^([A-Z_]+)=", line)
        if match and match.group(1) in values:
            line = f"{match.group(1)}={values[match.group(1)]}\n"
        sys.stdout.write(line)

print(f"DEMO_AUTH_PASSWORD={password}", file=sys.stderr)
