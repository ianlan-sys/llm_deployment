Set-StrictMode -Version Latest

function Invoke-DemoWithoutOpenAIKey {
    param([Parameter(Mandatory = $true)][scriptblock]$Action)

    $hadKey = Test-Path Env:DEMO_OPENAI_API_KEY
    $previousKey = $env:DEMO_OPENAI_API_KEY
    try {
        Remove-Item Env:DEMO_OPENAI_API_KEY -ErrorAction SilentlyContinue
        & $Action
    } finally {
        if ($hadKey) {
            $env:DEMO_OPENAI_API_KEY = $previousKey
        } else {
            Remove-Item Env:DEMO_OPENAI_API_KEY -ErrorAction SilentlyContinue
        }
    }
}

function Initialize-DemoAuthEnvironment {
    param(
        [Parameter(Mandatory = $true)][string]$CredentialGenerator
    )

    $authRequiredValue = [Environment]::GetEnvironmentVariable(
        'DEMO_AUTH_REQUIRED',
        [EnvironmentVariableTarget]::Process
    )
    $authRequired = -not $authRequiredValue -or (
        $authRequiredValue.ToLowerInvariant() -notin @('false', '0', 'no', 'off')
    )
    if (-not $authRequired) {
        return [pscustomobject]@{
            Generated = $false
            Username = $null
            Password = $null
        }
    }

    $authEnvironmentNames = @(
        'DEMO_AUTH_USERNAME',
        'DEMO_AUTH_PASSWORD_HASH',
        'DEMO_SESSION_SECRET'
    )
    $missingAuth = @(
        $authEnvironmentNames | Where-Object {
            [string]::IsNullOrWhiteSpace(
                [Environment]::GetEnvironmentVariable(
                    $_,
                    [EnvironmentVariableTarget]::Process
                )
            )
        }
    )
    if ($missingAuth.Count -eq 0) {
        return [pscustomobject]@{
            Generated = $false
            Username = $null
            Password = $null
        }
    }
    if ($missingAuth.Count -ne $authEnvironmentNames.Count) {
        throw "Authentication configuration is incomplete. Missing: $($missingAuth -join ', ')."
    }

    $generatedLines = @(& $CredentialGenerator)
    if ($LASTEXITCODE -ne 0) {
        throw 'Temporary credential generation failed.'
    }
    $generated = @{}
    foreach ($line in $generatedLines) {
        if ($line -notmatch "^([A-Z_]+)='([^']*)'$") {
            throw 'Credential generator returned an unexpected line.'
        }
        $generated[$Matches[1]] = $Matches[2]
    }
    $requiredGeneratedValues = @(
        'DEMO_AUTH_USERNAME',
        'DEMO_AUTH_PASSWORD',
        'DEMO_AUTH_PASSWORD_HASH',
        'DEMO_SESSION_SECRET'
    )
    foreach ($name in $requiredGeneratedValues) {
        if (-not $generated.ContainsKey($name) -or -not $generated[$name]) {
            throw "Credential generator did not return $name."
        }
    }
    foreach ($name in $authEnvironmentNames) {
        [Environment]::SetEnvironmentVariable(
            $name,
            $generated[$name],
            [EnvironmentVariableTarget]::Process
        )
    }

    return [pscustomobject]@{
        Generated = $true
        Username = $generated['DEMO_AUTH_USERNAME']
        Password = $generated['DEMO_AUTH_PASSWORD']
    }
}

function Resolve-DemoPython {
    $candidates = @()
    $python = Get-Command python.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($python) {
        $candidates += [pscustomobject]@{
            FilePath = $python.Source
            PrefixArguments = @()
        }
    }
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($launcher) {
        $candidates += [pscustomobject]@{
            FilePath = $launcher.Source
            PrefixArguments = @('-3.11')
        }
    }

    foreach ($candidate in $candidates) {
        $previousErrorActionPreference = $ErrorActionPreference
        try {
            $ErrorActionPreference = 'Continue'
            $version = & $candidate.FilePath @($candidate.PrefixArguments) -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
            $exitCode = $LASTEXITCODE
        } finally {
            $ErrorActionPreference = $previousErrorActionPreference
        }
        if ($exitCode -eq 0 -and $version -eq '3.11') {
            return $candidate
        }
    }
    throw 'Python 3.11 was not found. Install Python 3.11 or the Windows py launcher.'
}

function Resolve-DemoPnpm {
    $pnpm = Get-Command pnpm.cmd, pnpm.exe, pnpm -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($pnpm) {
        return [pscustomobject]@{ FilePath = $pnpm.Source; PrefixArguments = @() }
    }
    $corepack = Get-Command corepack.cmd, corepack.exe, corepack -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($corepack) {
        return [pscustomobject]@{ FilePath = $corepack.Source; PrefixArguments = @('pnpm') }
    }
    throw 'pnpm was not found. Install Node.js with Corepack support.'
}

function Test-DemoPythonModules {
    param(
        [Parameter(Mandatory = $true)]$Python,
        [Parameter(Mandatory = $true)][string[]]$Modules
    )

    $statement = 'import ' + ($Modules -join ', ')
    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        & $Python.FilePath @($Python.PrefixArguments) -c $statement 2>$null
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    return $exitCode -eq 0
}

function Get-DemoProcessInfo {
    param([Parameter(Mandatory = $true)][int]$ProcessId)

    return Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction SilentlyContinue
}

function Test-DemoProcessOwnership {
    param(
        [Parameter(Mandatory = $true)]$StoredProcess,
        [Parameter(Mandatory = $false)]$CurrentProcess,
        [Parameter(Mandatory = $true)][string[]]$RequiredMarkers
    )

    if ($null -eq $CurrentProcess) { return $false }
    if ([int]$StoredProcess.pid -ne [int]$CurrentProcess.ProcessId) { return $false }
    if (-not [string]::Equals(
        [string]$StoredProcess.executablePath,
        [string]$CurrentProcess.ExecutablePath,
        [StringComparison]::OrdinalIgnoreCase
    )) { return $false }
    if (-not [string]::Equals(
        [string]$StoredProcess.commandLine,
        [string]$CurrentProcess.CommandLine,
        [StringComparison]::Ordinal
    )) { return $false }
    foreach ($marker in $RequiredMarkers) {
        if (-not ([string]$CurrentProcess.CommandLine).Contains($marker)) { return $false }
    }
    return $true
}

function Get-DemoProcessRecord {
    param(
        [Parameter(Mandatory = $true)][int]$ProcessId,
        [Parameter(Mandatory = $true)][string[]]$RequiredMarkers,
        [switch]$PreferChildProcess
    )

    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        $rootProcess = Get-DemoProcessInfo -ProcessId $ProcessId
        $childProcesses = @(Get-CimInstance Win32_Process -Filter "ParentProcessId = $ProcessId" -ErrorAction SilentlyContinue)
        $candidates = if ($PreferChildProcess) { @($childProcesses) } else { @($rootProcess) + @($childProcesses) }
        foreach ($process in $candidates) {
            if (-not $process -or -not $process.ExecutablePath -or -not $process.CommandLine) { continue }
            $hasMarkers = $true
            foreach ($marker in $RequiredMarkers) {
                if (-not ([string]$process.CommandLine).Contains($marker)) {
                    $hasMarkers = $false
                    break
                }
            }
            if ($hasMarkers) {
                return [pscustomobject]@{
                    pid = [int]$process.ProcessId
                    executablePath = [string]$process.ExecutablePath
                    commandLine = [string]$process.CommandLine
                }
            }
        }
        Start-Sleep -Milliseconds 100
    }
    throw "Process $ProcessId did not expose the expected command line."
}

function Wait-DemoEndpoint {
    param(
        [Parameter(Mandatory = $true)][string]$Uri,
        [int]$TimeoutSeconds = 45
    )

    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        try {
            $response = Invoke-WebRequest -Uri $Uri -UseBasicParsing -TimeoutSec 3
            if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 300) { return }
        } catch {
            Start-Sleep -Milliseconds 500
        }
    } while ([DateTime]::UtcNow -lt $deadline)
    throw "Timed out waiting for $Uri"
}

function Stop-DemoProcessRecord {
    param(
        [Parameter(Mandatory = $true)]$StoredProcess,
        [Parameter(Mandatory = $true)][string[]]$RequiredMarkers
    )

    $current = Get-DemoProcessInfo -ProcessId ([int]$StoredProcess.pid)
    if (-not $current) { return $false }
    if (-not (Test-DemoProcessOwnership -StoredProcess $StoredProcess -CurrentProcess $current -RequiredMarkers $RequiredMarkers)) {
        throw "PID $($StoredProcess.pid) no longer matches the stored executable and command line; it was not stopped."
    }
    Stop-Process -Id ([int]$StoredProcess.pid) -ErrorAction Stop
    return $true
}
