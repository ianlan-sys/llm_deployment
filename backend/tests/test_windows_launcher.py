import base64
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from app.auth import verify_scrypt_hash


ROOT = Path(__file__).resolve().parents[2]
HELPERS = ROOT / "scripts" / "demo-processes.ps1"
CREDENTIAL_GENERATOR = ROOT / "scripts" / "generate-demo-auth.ps1"
ENV_INITIALIZER = ROOT / "scripts" / "initialize-demo-env.ps1"


def run_powershell(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            script,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


def test_python_resolver_finds_python_311() -> None:
    result = run_powershell(
        "$ErrorActionPreference = 'Stop'; "
        f". '{HELPERS}'; "
        "$python = Resolve-DemoPython; "
        "$version = & $python.FilePath @($python.PrefixArguments) "
        "-c \"import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')\"; "
        "[pscustomobject]@{ FilePath = $python.FilePath; Version = $version } "
        "| ConvertTo-Json -Compress"
    )

    assert result.returncode == 0, result.stderr
    resolved = json.loads(result.stdout.strip())
    assert resolved["Version"] == "3.11"
    assert Path(resolved["FilePath"]).name.lower() in {"python.exe", "py.exe"}


def test_process_ownership_requires_exact_stored_identity_and_markers() -> None:
    result = run_powershell(
        f". '{HELPERS}'; "
        "$stored = [pscustomobject]@{ "
        "pid = 4242; executablePath = 'C:\\Python311\\python.exe'; "
        "commandLine = '\"C:\\Python311\\python.exe\" -m uvicorn app.main:app --port 8000' }; "
        "$matching = [pscustomobject]@{ ProcessId = 4242; "
        "ExecutablePath = 'C:\\Python311\\python.exe'; "
        "CommandLine = '\"C:\\Python311\\python.exe\" -m uvicorn app.main:app --port 8000' }; "
        "$wrongCommand = [pscustomobject]@{ ProcessId = 4242; "
        "ExecutablePath = 'C:\\Python311\\python.exe'; "
        "CommandLine = '\"C:\\Python311\\python.exe\" -m http.server 8000' }; "
        "$required = @('-m', 'uvicorn', 'app.main:app', '--port', '8000'); "
        "[pscustomobject]@{ "
        "matching = Test-DemoProcessOwnership -StoredProcess $stored "
        "-CurrentProcess $matching -RequiredMarkers $required; "
        "wrongCommand = Test-DemoProcessOwnership -StoredProcess $stored "
        "-CurrentProcess $wrongCommand -RequiredMarkers $required } "
        "| ConvertTo-Json -Compress"
    )

    assert result.returncode == 0, result.stderr
    ownership = json.loads(result.stdout.strip())
    assert ownership == {"matching": True, "wrongCommand": False}


def test_dependency_probe_returns_false_instead_of_throwing_on_missing_module() -> None:
    result = run_powershell(
        "$ErrorActionPreference = 'Stop'; "
        f". '{HELPERS}'; "
        "$python = Resolve-DemoPython; "
        "[pscustomobject]@{ "
        "available = Test-DemoPythonModules -Python $python -Modules @('json'); "
        "missing = Test-DemoPythonModules -Python $python "
        "-Modules @('module_that_does_not_exist_for_task_12') } "
        "| ConvertTo-Json -Compress"
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip()) == {"available": True, "missing": False}


def test_child_tracking_never_falls_back_to_a_matching_root_process() -> None:
    result = run_powershell(
        f". '{HELPERS}'; "
        "$rejectedRoot = $false; "
        "try { "
        "$null = Get-DemoProcessRecord -ProcessId $PID "
        "-RequiredMarkers @('task12_root_only_marker') -PreferChildProcess "
        "} catch { $rejectedRoot = $true }; "
        "$rejectedRoot | ConvertTo-Json -Compress # task12_root_only_marker"
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip()) is True


def test_frontend_child_does_not_inherit_model_secret_and_parent_is_restored() -> None:
    result = run_powershell(
        "$ErrorActionPreference = 'Stop'; "
        f". '{HELPERS}'; "
        "$env:DEMO_OPENAI_API_KEY = 'parent-server-secret'; "
        "$child = Invoke-DemoWithoutOpenAIKey { "
        "node.exe -e \"console.log(process.env.DEMO_OPENAI_API_KEY || 'absent')\" }; "
        "[pscustomobject]@{ child = [string]$child; parent = $env:DEMO_OPENAI_API_KEY } "
        "| ConvertTo-Json -Compress"
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip()) == {
        "child": "absent",
        "parent": "parent-server-secret",
    }


def test_credential_generator_prints_compatible_secrets_without_writing_files(
    tmp_path: Path,
) -> None:
    before = {path.relative_to(tmp_path) for path in tmp_path.rglob("*")}
    result = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(CREDENTIAL_GENERATOR),
            "-Username",
            "operator-demo",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )

    assert result.returncode == 0, result.stderr
    lines = [line for line in result.stdout.splitlines() if line]
    assert len(lines) == 4
    assignments = dict(line.split("=", 1) for line in lines)
    assert all(
        value.startswith("'") and value.endswith("'")
        for value in assignments.values()
    )
    values = {key: value[1:-1] for key, value in assignments.items()}
    assert set(values) == {
        "DEMO_AUTH_USERNAME",
        "DEMO_AUTH_PASSWORD",
        "DEMO_AUTH_PASSWORD_HASH",
        "DEMO_SESSION_SECRET",
    }
    assert values["DEMO_AUTH_USERNAME"] == "operator-demo"
    assert len(values["DEMO_AUTH_PASSWORD"]) == 20
    assert result.stdout.count(values["DEMO_AUTH_PASSWORD"]) == 1

    hash_parts = values["DEMO_AUTH_PASSWORD_HASH"].split("$")
    assert hash_parts[:4] == ["scrypt", "16384", "8", "1"]
    assert len(bytes.fromhex(hash_parts[4])) == 16
    assert len(bytes.fromhex(hash_parts[5])) == 32
    assert verify_scrypt_hash(
        values["DEMO_AUTH_PASSWORD"], values["DEMO_AUTH_PASSWORD_HASH"]
    )

    encoded_secret = values["DEMO_SESSION_SECRET"]
    padding = "=" * (-len(encoded_secret) % 4)
    assert len(base64.urlsafe_b64decode(encoded_secret + padding)) == 32
    after = {path.relative_to(tmp_path) for path in tmp_path.rglob("*")}
    assert after == before


def test_compose_validates_the_same_auth_values_that_reach_backend(
    tmp_path: Path,
) -> None:
    if shutil.which("docker") is None:
        import pytest

        pytest.skip("Docker Compose CLI is not installed")

    generated = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(CREDENTIAL_GENERATOR),
            "-Username",
            "compose-operator",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert generated.returncode == 0
    quoted_assignments = dict(
        line.split("=", 1) for line in generated.stdout.splitlines() if line
    )
    password = quoted_assignments.pop("DEMO_AUTH_PASSWORD")[1:-1]
    generated_values = {
        key: value[1:-1] for key, value in quoted_assignments.items()
    }
    service_env_file = tmp_path / "backend.env"
    service_env_file.write_text(
        "DEMO_AUTH_USERNAME='unvalidated-env-file-user'\n"
        "DEMO_AUTH_PASSWORD_HASH=''\n"
        "DEMO_SESSION_SECRET='unvalidated-env-file-secret'\n",
        encoding="utf-8",
    )
    interpolation_env_file = tmp_path / "compose.env"
    interpolation_env_file.write_text(
        "DEMO_AUTH_REQUIRED=true\n"
        f"COMPOSE_ENV_FILE_PATH='{service_env_file}'\n",
        encoding="utf-8",
    )
    compose_process_environment = os.environ.copy()
    compose_process_environment.update(generated_values)

    configured = subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            str(interpolation_env_file),
            "-f",
            str(ROOT / "docker-compose.yml"),
            "config",
            "--format",
            "json",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=compose_process_environment,
        check=False,
    )
    assert configured.returncode == 0
    compose = json.loads(configured.stdout)
    backend_environment = compose["services"]["backend"]["environment"]
    assert backend_environment["DEMO_AUTH_USERNAME"] == generated_values[
        "DEMO_AUTH_USERNAME"
    ]
    assert backend_environment["DEMO_SESSION_SECRET"] == generated_values[
        "DEMO_SESSION_SECRET"
    ]

    rendered_hash = backend_environment["DEMO_AUTH_PASSWORD_HASH"]
    # ``docker compose config`` serializes each literal container dollar as an
    # escaped ``$$``. Reject any unpaired dollar before decoding that notation.
    assert re.search(r"(?<!\$)\$(?!\$)", rendered_hash) is None
    container_hash = rendered_hash.replace("$$", "$")
    assert container_hash == generated_values["DEMO_AUTH_PASSWORD_HASH"]
    assert verify_scrypt_hash(password, container_hash)


def test_credential_generator_has_no_disk_secret_writer() -> None:
    source = CREDENTIAL_GENERATOR.read_text(encoding="utf-8")

    assert "Set-Content" not in source
    assert "Out-File" not in source
    assert "Add-Content" not in source
    assert ".env" not in source


def test_compose_env_initializer_creates_login_ready_env_without_plaintext_password(
    tmp_path: Path,
) -> None:
    env_path = tmp_path / ".env"
    result = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(ENV_INITIALIZER),
            "-Path",
            str(env_path),
            "-Username",
            "quickstart-operator",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert env_path.is_file()
    assignments = dict(
        line.split("=", 1)
        for line in env_path.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#") and "=" in line
    )
    assert assignments["DEMO_AUTH_REQUIRED"] == "true"
    assert assignments["DEMO_AUTH_USERNAME"] == "'quickstart-operator'"
    assert assignments["DEMO_AUTH_PASSWORD_HASH"].startswith("'scrypt$")
    assert assignments["DEMO_SESSION_SECRET"].startswith("'")
    assert "DEMO_AUTH_PASSWORD" not in assignments

    output_assignments = dict(
        line.split("=", 1)
        for line in result.stdout.splitlines()
        if line.startswith("DEMO_AUTH_") and "=" in line
    )
    assert set(output_assignments) == {
        "DEMO_AUTH_USERNAME",
        "DEMO_AUTH_PASSWORD",
    }
    plaintext_password = output_assignments["DEMO_AUTH_PASSWORD"]
    assert plaintext_password
    assert result.stdout.count(plaintext_password) == 1
    assert plaintext_password not in env_path.read_text(encoding="utf-8")
    assert verify_scrypt_hash(
        plaintext_password,
        assignments["DEMO_AUTH_PASSWORD_HASH"][1:-1],
    )


def test_compose_env_initializer_refuses_to_overwrite_existing_env(
    tmp_path: Path,
) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("KEEP=existing\n", encoding="utf-8")

    result = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(ENV_INITIALIZER),
            "-Path",
            str(env_path),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )

    assert result.returncode != 0
    assert "Refusing to overwrite existing environment file" in result.stderr
    assert env_path.read_text(encoding="utf-8") == "KEEP=existing\n"


def test_auth_bootstrap_generates_credentials_only_when_all_values_are_missing() -> None:
    result = run_powershell(
        "$ErrorActionPreference = 'Stop'; "
        f". '{HELPERS}'; "
        "$env:DEMO_AUTH_REQUIRED = 'true'; "
        "Remove-Item Env:DEMO_AUTH_USERNAME -ErrorAction SilentlyContinue; "
        "Remove-Item Env:DEMO_AUTH_PASSWORD_HASH -ErrorAction SilentlyContinue; "
        "Remove-Item Env:DEMO_SESSION_SECRET -ErrorAction SilentlyContinue; "
        f"$bootstrap = Initialize-DemoAuthEnvironment -CredentialGenerator '{CREDENTIAL_GENERATOR}'; "
        "[pscustomobject]@{ "
        "generated = $bootstrap.Generated; "
        "username = $env:DEMO_AUTH_USERNAME; "
        "passwordHash = $env:DEMO_AUTH_PASSWORD_HASH; "
        "sessionSecret = $env:DEMO_SESSION_SECRET; "
        "password = $bootstrap.Password "
        "} | ConvertTo-Json -Compress"
    )

    assert result.returncode == 0, result.stderr
    bootstrap = json.loads(result.stdout.strip())
    assert bootstrap["generated"] is True
    assert bootstrap["username"] == "demo-operator"
    assert bootstrap["sessionSecret"]
    assert verify_scrypt_hash(
        bootstrap["password"],
        bootstrap["passwordHash"],
    )


def test_auth_bootstrap_preserves_complete_explicit_credentials() -> None:
    missing_generator = ROOT / "scripts" / "must-not-run.ps1"
    result = run_powershell(
        "$ErrorActionPreference = 'Stop'; "
        f". '{HELPERS}'; "
        "$env:DEMO_AUTH_REQUIRED = 'true'; "
        "$env:DEMO_AUTH_USERNAME = 'existing-user'; "
        "$env:DEMO_AUTH_PASSWORD_HASH = 'existing-hash'; "
        "$env:DEMO_SESSION_SECRET = 'existing-secret'; "
        f"$bootstrap = Initialize-DemoAuthEnvironment -CredentialGenerator '{missing_generator}'; "
        "[pscustomobject]@{ "
        "generated = $bootstrap.Generated; "
        "username = $env:DEMO_AUTH_USERNAME; "
        "passwordHash = $env:DEMO_AUTH_PASSWORD_HASH; "
        "sessionSecret = $env:DEMO_SESSION_SECRET "
        "} | ConvertTo-Json -Compress"
    )

    assert result.returncode == 0, result.stderr
    bootstrap = json.loads(result.stdout.strip())
    assert bootstrap == {
        "generated": False,
        "username": "existing-user",
        "passwordHash": "existing-hash",
        "sessionSecret": "existing-secret",
    }


def test_auth_bootstrap_rejects_partial_explicit_credentials_with_missing_names() -> None:
    missing_generator = ROOT / "scripts" / "must-not-run.ps1"
    result = run_powershell(
        "$ErrorActionPreference = 'Stop'; "
        f". '{HELPERS}'; "
        "$env:DEMO_AUTH_REQUIRED = 'true'; "
        "$env:DEMO_AUTH_USERNAME = 'partial-user'; "
        "Remove-Item Env:DEMO_AUTH_PASSWORD_HASH -ErrorAction SilentlyContinue; "
        "Remove-Item Env:DEMO_SESSION_SECRET -ErrorAction SilentlyContinue; "
        f"Initialize-DemoAuthEnvironment -CredentialGenerator '{missing_generator}'"
    )

    assert result.returncode != 0
    assert "Authentication configuration is incomplete" in result.stderr
    assert "DEMO_AUTH_PASSWORD_HASH" in result.stderr
    assert "DEMO_SESSION_SECRET" in result.stderr
    assert "DEMO_AUTH_USERNAME" not in result.stderr


def test_windows_quickstart_uses_the_tested_auth_bootstrap_without_disabling_it() -> None:
    source = (ROOT / "scripts" / "start-demo.ps1").read_text(encoding="utf-8")

    assert "Initialize-DemoAuthEnvironment" in source
    assert "DEMO_AUTH_REQUIRED = 'false'" not in source


def test_readme_quickstarts_prepare_auth_before_starting() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    windows = readme[readme.index("## Windows 一键启动") : readme.index("## Docker Compose")]
    compose = readme[readme.index("## Docker Compose") : readme.index("## 演示流程")]

    assert "临时登录凭据" in windows
    assert "scripts\\start-demo.ps1" in windows
    assert "scripts\\initialize-demo-env.ps1" in compose
    assert compose.index("scripts\\initialize-demo-env.ps1") < compose.index(
        "docker compose up --build -d"
    )
    assert "明文密码" in compose
