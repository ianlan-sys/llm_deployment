$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repositoryRoot = Split-Path -Parent $PSScriptRoot
$pidFile = Join-Path $repositoryRoot 'data\runtime\processes.json'
. (Join-Path $PSScriptRoot 'demo-processes.ps1')

if (-not (Test-Path -LiteralPath $pidFile)) {
    Write-Host 'The demo is not running: no PID file was found.'
    exit 0
}

$runtime = Get-Content -LiteralPath $pidFile -Raw -Encoding UTF8 | ConvertFrom-Json
if (-not $runtime.backend -or -not $runtime.frontend) {
    throw "The PID file is invalid and was left in place: $pidFile"
}

$errors = @()
$targets = @(
    [pscustomobject]@{ Name = 'frontend'; Record = $runtime.frontend; Markers = @('vite.js', '--host', '127.0.0.1', '--port', '5173') },
    [pscustomobject]@{ Name = 'backend'; Record = $runtime.backend; Markers = @('-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', '8000') }
)
foreach ($target in $targets) {
    try {
        $stopped = Stop-DemoProcessRecord -StoredProcess $target.Record -RequiredMarkers $target.Markers
        if ($stopped) { Write-Host "Stopped $($target.Name) process $($target.Record.pid)." }
        else { Write-Host "$($target.Name) process $($target.Record.pid) had already exited." }
    } catch {
        $errors += $_.Exception.Message
        Write-Warning $_.Exception.Message
    }
}

if ($errors.Count -gt 0) {
    throw ('One or more stored processes were not stopped safely; the PID file was retained for retry: ' + ($errors -join ' '))
}
Remove-Item -LiteralPath $pidFile -Force
Write-Host "Removed PID file: $pidFile"
