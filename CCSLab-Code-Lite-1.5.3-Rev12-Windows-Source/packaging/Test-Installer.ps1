param(
    [Parameter(Mandatory=$true)][string]$Installer,
    [string]$TestRoot = ''
)

$ErrorActionPreference = 'Stop'
if (-not $TestRoot) {
    $TestRoot = Join-Path (Split-Path (Split-Path $PSScriptRoot)) '.work\installer-smoke'
}
$Installer = (Resolve-Path -LiteralPath $Installer).Path
$TestRoot = (New-Item -ItemType Directory -Path $TestRoot -Force).FullName
$Test = (New-Item -ItemType Directory -Path (Join-Path $TestRoot ([guid]::NewGuid().ToString('N')))).FullName
$InstallDir = Join-Path $Test ('CCSLab Labcoding ' + [char]0x5B89 + [char]0x88C5)
$DataDir = Join-Path $Test ('app data ' + [char]0x5386 + [char]0x53F2)

function Invoke-Checked([string]$File, [string[]]$Arguments, [int]$ExpectedExit = 0) {
    $Process = Start-Process -FilePath $File -ArgumentList $Arguments -PassThru -WindowStyle Hidden
    try {
        if (!$Process.WaitForExit(65000)) {
            Stop-Process -Id $Process.Id -Force
            throw "Smoke test timed out: $File"
        }
        if ($Process.ExitCode -ne $ExpectedExit) {
            throw "Smoke test exited with $($Process.ExitCode), expected ${ExpectedExit}: $File"
        }
    } finally {
        $Process.Dispose()
    }
}

New-Item -ItemType Directory -Path $InstallDir | Out-Null
Invoke-Checked $Installer @('--smoke-install', "`"$InstallDir`"")
# The same registered program directory supports in-place upgrades.
Invoke-Checked $Installer @('--smoke-install', "`"$InstallDir`"")
$Exe = Join-Path $InstallDir 'CCSLab Labcoding.exe'
$OpenCode = Join-Path $InstallDir 'vendor\opencode.exe'
$Expected = @($Exe, $OpenCode, (Join-Path $InstallDir 'Uninstall.exe'), (Join-Path $InstallDir 'vendor\LICENSE-OpenCode.txt'))
foreach ($Path in $Expected) {
    if (!(Test-Path -LiteralPath $Path -PathType Leaf)) { throw "Expected installed file is missing: $Path" }
}
Invoke-Checked $Exe @('--smoke-test', '--data-dir', "`"$DataDir`"")

$OccupiedDir = Join-Path $Test 'occupied'
New-Item -ItemType Directory -Path $OccupiedDir | Out-Null
$Keep = Join-Path $OccupiedDir 'user-file.txt'
Set-Content -LiteralPath $Keep -Value 'keep'
Invoke-Checked $Installer @('--smoke-install', "`"$OccupiedDir`"") 1
if ((Get-Content -LiteralPath $Keep) -ne 'keep' -or
    (Test-Path -LiteralPath (Join-Path $OccupiedDir 'CCSLab Labcoding.exe'))) {
    throw 'An occupied directory was modified.'
}

$env:XDG_DATA_HOME = Join-Path $Test 'xdg-data'
$env:XDG_CONFIG_HOME = Join-Path $Test 'xdg-config'
$env:XDG_CACHE_HOME = Join-Path $Test 'xdg-cache'
$env:XDG_STATE_HOME = Join-Path $Test 'xdg-state'
foreach ($HomeDir in @($env:XDG_DATA_HOME, $env:XDG_CONFIG_HOME, $env:XDG_CACHE_HOME, $env:XDG_STATE_HOME)) {
    New-Item -ItemType Directory -Path (Join-Path $HomeDir 'opencode') -Force | Out-Null
}
$Version = & $OpenCode --version | Select-Object -Last 1
if ($LASTEXITCODE -ne 0 -or $Version -ne '1.17.16') {
    throw "Bundled OpenCode did not report version 1.17.16."
}
"Custom empty folder install, in-place upgrade and Tk startup passed."
"Nonempty unrelated folder correctly refused; existing file preserved."
"Bundled OpenCode: $Version"
"Test installation: $InstallDir"
