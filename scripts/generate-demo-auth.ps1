param(
    [ValidatePattern('^[A-Za-z0-9._-]{1,64}$')]
    [string]$Username = 'demo-operator'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$helperScript = Join-Path $PSScriptRoot 'demo-processes.ps1'
. $helperScript
$python = Resolve-DemoPython

$program = @'
import base64
import hashlib
import secrets
import string
import sys

username = sys.argv[2]
alphabet = string.ascii_letters + string.digits
password = "".join(secrets.choice(alphabet) for _ in range(20))
salt = secrets.token_bytes(16)
digest = hashlib.scrypt(
    password.encode("utf-8"),
    salt=salt,
    n=2**14,
    r=8,
    p=1,
    dklen=32,
)
password_hash = f"scrypt$16384$8$1${salt.hex()}${digest.hex()}"
session_secret = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")

print(f"DEMO_AUTH_USERNAME='{username}'")
print(f"DEMO_AUTH_PASSWORD='{password}'")
print(f"DEMO_AUTH_PASSWORD_HASH='{password_hash}'")
print(f"DEMO_SESSION_SECRET='{session_secret}'")
'@

$encodedProgram = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($program))
$launcher = 'import base64,sys;exec(base64.b64decode(sys.argv[1]))'
& $python.FilePath @($python.PrefixArguments) -c $launcher $encodedProgram $Username
if ($LASTEXITCODE -ne 0) {
    throw 'Credential generation failed.'
}
