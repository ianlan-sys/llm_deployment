param(
    [string]$Path,
    [ValidatePattern('^[A-Za-z0-9._-]{1,64}$')]
    [string]$Username = 'demo-operator'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repositoryRoot = Split-Path -Parent $PSScriptRoot
if (-not $Path) {
    $Path = Join-Path $repositoryRoot '.env'
}
$targetPath = [IO.Path]::GetFullPath($Path)
if (Test-Path -LiteralPath $targetPath) {
    throw "Refusing to overwrite existing environment file: $targetPath"
}

$templatePath = Join-Path $repositoryRoot '.env.example'
$credentialGenerator = Join-Path $PSScriptRoot 'generate-demo-auth.ps1'
$generatedLines = @(& $credentialGenerator -Username $Username)
if ($LASTEXITCODE -ne 0) {
    throw 'Credential generation failed.'
}

$generated = @{}
foreach ($line in $generatedLines) {
    if ($line -notmatch "^([A-Z_]+)='([^']*)'$") {
        throw 'Credential generator returned an unexpected line.'
    }
    $generated[$Matches[1]] = $Matches[2]
}

$required = @(
    'DEMO_AUTH_USERNAME',
    'DEMO_AUTH_PASSWORD',
    'DEMO_AUTH_PASSWORD_HASH',
    'DEMO_SESSION_SECRET'
)
foreach ($name in $required) {
    if (-not $generated.ContainsKey($name) -or -not $generated[$name]) {
        throw "Credential generator did not return $name."
    }
}

$replacementValues = @{
    DEMO_AUTH_REQUIRED = 'true'
    DEMO_AUTH_USERNAME = "'" + $generated['DEMO_AUTH_USERNAME'] + "'"
    DEMO_AUTH_PASSWORD_HASH = "'" + $generated['DEMO_AUTH_PASSWORD_HASH'] + "'"
    DEMO_SESSION_SECRET = "'" + $generated['DEMO_SESSION_SECRET'] + "'"
}
$renderedLines = foreach ($line in [IO.File]::ReadAllLines($templatePath)) {
    if ($line -match '^([A-Z_]+)=') {
        $name = $Matches[1]
        if ($replacementValues.ContainsKey($name)) {
            "$name=$($replacementValues[$name])"
            continue
        }
    }
    $line
}

$targetDirectory = Split-Path -Parent $targetPath
if (-not (Test-Path -LiteralPath $targetDirectory)) {
    [IO.Directory]::CreateDirectory($targetDirectory) | Out-Null
}
$temporaryPath = "$targetPath.tmp.$PID"
try {
    [IO.File]::WriteAllLines(
        $temporaryPath,
        $renderedLines,
        [Text.UTF8Encoding]::new($false)
    )
    [IO.File]::Move($temporaryPath, $targetPath)
} finally {
    if (Test-Path -LiteralPath $temporaryPath) {
        Remove-Item -LiteralPath $temporaryPath -Force
    }
}

Write-Output "DEMO_AUTH_USERNAME=$($generated['DEMO_AUTH_USERNAME'])"
Write-Output "DEMO_AUTH_PASSWORD=$($generated['DEMO_AUTH_PASSWORD'])"
Write-Host "Authentication settings were written to $targetPath. Keep the password above for login; it was not written to disk."
