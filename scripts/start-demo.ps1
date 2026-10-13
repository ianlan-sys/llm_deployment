param([switch]$NoBrowser)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repositoryRoot = Split-Path -Parent $PSScriptRoot
$backendRoot = Join-Path $repositoryRoot 'backend'
$frontendRoot = Join-Path $repositoryRoot 'frontend'
$runtimeRoot = Join-Path $repositoryRoot 'data\runtime'
$pidFile = Join-Path $runtimeRoot 'processes.json'
$helperScript = Join-Path $PSScriptRoot 'demo-processes.ps1'
. $helperScript

if (Test-Path -LiteralPath $pidFile) {
    throw "A runtime PID file already exists at $pidFile. Run scripts\stop-demo.ps1 before starting again."
}

$python = Resolve-DemoPython
$pnpm = Resolve-DemoPnpm
$node = Get-Command node.exe -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $node) { throw 'Node.js was not found. Install Node.js 22 or newer.' }

New-Item -ItemType Directory -Path $runtimeRoot -Force | Out-Null
$backendLog = Join-Path $runtimeRoot 'backend.stdout.log'
$backendErrorLog = Join-Path $runtimeRoot 'backend.stderr.log'
$frontendLog = Join-Path $runtimeRoot 'frontend.stdout.log'
$frontendErrorLog = Join-Path $runtimeRoot 'frontend.stderr.log'

Push-Location $backendRoot
try {
    $backendModules = @('fastapi', 'httpx', 'multipart', 'numpy', 'pydantic_settings', 'pypdf', 'sqlalchemy', 'uvicorn', 'docx', 'app.main')
    if (-not (Test-DemoPythonModules -Python $python -Modules $backendModules)) {
        Write-Host 'First run: installing backend dependencies. This can take several minutes.'
        & $python.FilePath @($python.PrefixArguments) -m pip install -e "${backendRoot}[dev]"
        if ($LASTEXITCODE -ne 0) { throw 'Backend dependency installation failed.' }
    }
} finally {
    Pop-Location
}

$viteScript = Join-Path $frontendRoot 'node_modules\vite\bin\vite.js'
$frontendPackages = @('@tanstack\react-query', '@vitejs\plugin-react', 'react', 'react-dom', 'react-router-dom', 'vite', 'vitest')
$missingFrontendPackages = @($frontendPackages | Where-Object { -not (Test-Path -LiteralPath (Join-Path $frontendRoot "node_modules\$_\package.json")) })
if (-not (Test-Path -LiteralPath $viteScript) -or $missingFrontendPackages.Count -gt 0) {
    Write-Host 'First run: installing frontend dependencies. This can take several minutes.'
    Push-Location $frontendRoot
    try {
        & $pnpm.FilePath @($pnpm.PrefixArguments) install --frozen-lockfile
        if ($LASTEXITCODE -ne 0) { throw 'Frontend dependency installation failed.' }
    } finally {
        Pop-Location
    }
}

$backendProcess = $null
$frontendProcess = $null
$backendRecord = $null
$frontendRecord = $null
$backendMarkers = @('-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', '8000')
$frontendMarkers = @('vite.js', '--host', '127.0.0.1', '--port', '5173')
$previousRuntimeDir = $env:DEMO_RUNTIME_DIR
$authEnvironmentNames = @(
    'DEMO_AUTH_USERNAME',
    'DEMO_AUTH_PASSWORD_HASH',
    'DEMO_SESSION_SECRET'
)
$previousAuthEnvironment = @{}
foreach ($name in $authEnvironmentNames) {
    $previousAuthEnvironment[$name] = [Environment]::GetEnvironmentVariable(
        $name,
        [EnvironmentVariableTarget]::Process
    )
}

$credentialGenerator = Join-Path $PSScriptRoot 'generate-demo-auth.ps1'
$authBootstrap = Initialize-DemoAuthEnvironment -CredentialGenerator $credentialGenerator
if ($authBootstrap.Generated) {
    Write-Host 'Generated temporary login credentials for this Demo run:'
    Write-Host "DEMO_AUTH_USERNAME=$($authBootstrap.Username)"
    Write-Host "DEMO_AUTH_PASSWORD=$($authBootstrap.Password)"
}
$pythonIsLauncher = [IO.Path]::GetFileName($python.FilePath) -ieq 'py.exe'
try {
    $env:DEMO_RUNTIME_DIR = $runtimeRoot
    $backendArguments = @($python.PrefixArguments) + @('-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', '8000')
    $backendProcess = Start-Process -FilePath $python.FilePath -ArgumentList $backendArguments -WorkingDirectory $backendRoot -RedirectStandardOutput $backendLog -RedirectStandardError $backendErrorLog -WindowStyle Hidden -PassThru
} finally {
    $env:DEMO_RUNTIME_DIR = $previousRuntimeDir
    foreach ($name in $authEnvironmentNames) {
        [Environment]::SetEnvironmentVariable(
            $name,
            $previousAuthEnvironment[$name],
            [EnvironmentVariableTarget]::Process
        )
    }
}

try {
    $quotedViteScript = '"' + $viteScript + '"'
    $frontendProcess = Invoke-DemoWithoutOpenAIKey {
        Start-Process -FilePath $node.Source -ArgumentList @($quotedViteScript, '--host', '127.0.0.1', '--port', '5173') -WorkingDirectory $frontendRoot -RedirectStandardOutput $frontendLog -RedirectStandardError $frontendErrorLog -WindowStyle Hidden -PassThru
    }
    $backendRecord = Get-DemoProcessRecord -ProcessId $backendProcess.Id -RequiredMarkers $backendMarkers -PreferChildProcess:$pythonIsLauncher
    $frontendRecord = Get-DemoProcessRecord -ProcessId $frontendProcess.Id -RequiredMarkers $frontendMarkers
    Wait-DemoEndpoint -Uri 'http://127.0.0.1:8000/api/health'
    Wait-DemoEndpoint -Uri 'http://127.0.0.1:5173/api/health'

    [pscustomobject]@{
        version = 1
        startedAt = [DateTime]::UtcNow.ToString('o')
        backend = $backendRecord
        frontend = $frontendRecord
    } | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $pidFile -Encoding UTF8
} catch {
    if ($frontendRecord) {
        try { Stop-DemoProcessRecord -StoredProcess $frontendRecord -RequiredMarkers $frontendMarkers | Out-Null } catch { Write-Warning $_ }
    } elseif ($frontendProcess) {
        try {
            $frontendRecord = Get-DemoProcessRecord -ProcessId $frontendProcess.Id -RequiredMarkers $frontendMarkers
            Stop-DemoProcessRecord -StoredProcess $frontendRecord -RequiredMarkers $frontendMarkers | Out-Null
        } catch { Write-Warning "Frontend cleanup was skipped because ownership could not be validated: $_" }
    }
    if ($backendRecord) {
        try { Stop-DemoProcessRecord -StoredProcess $backendRecord -RequiredMarkers $backendMarkers | Out-Null } catch { Write-Warning $_ }
    } elseif ($backendProcess) {
        try {
            $backendRecord = Get-DemoProcessRecord -ProcessId $backendProcess.Id -RequiredMarkers $backendMarkers -PreferChildProcess:$pythonIsLauncher
            Stop-DemoProcessRecord -StoredProcess $backendRecord -RequiredMarkers $backendMarkers | Out-Null
        } catch { Write-Warning "Backend cleanup was skipped because ownership could not be validated: $_" }
    }
    throw
}

Write-Host 'Demo started: http://localhost:5173'
Write-Host "Runtime metadata: $pidFile"
if (-not $NoBrowser) {
    Start-Process 'http://localhost:5173'
}
