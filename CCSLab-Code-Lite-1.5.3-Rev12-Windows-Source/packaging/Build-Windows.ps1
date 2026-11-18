param(
    [string]$Output = (Join-Path (Split-Path (Split-Path $PSScriptRoot)) '.work\releases\CCSLab-Labcoding-Test-Setup-x64.exe'),
    [string]$Python = 'python',
    [string]$PyInstallerPath = '',
    [string]$BuildRoot = (Join-Path (Split-Path (Split-Path $PSScriptRoot)) ('.work\build\test-' + (Get-Date -Format 'yyyyMMdd-HHmmss')))
)

$ErrorActionPreference = 'Stop'
$LocalWork = Join-Path (Split-Path (Split-Path $PSScriptRoot)) '.work'
$BuildRoot = [System.IO.Path]::GetFullPath($BuildRoot)
$Output = [System.IO.Path]::GetFullPath($Output)
$FixedTestOutput = [System.IO.Path]::GetFullPath((Join-Path $LocalWork 'releases\CCSLab-Labcoding-Test-Setup-x64.exe'))
$VersionInfo = Get-Content -LiteralPath (Join-Path (Split-Path $PSScriptRoot) 'app\version.json') -Raw -Encoding utf8 | ConvertFrom-Json
if ($VersionInfo.version -notmatch '^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$') {
    throw "Invalid semantic version for installer output: $($VersionInfo.version)"
}
$VersionedTestOutput = [System.IO.Path]::GetFullPath((Join-Path $LocalWork `
    ('releases\CCSLab-Labcoding-' + $VersionInfo.version + '-Setup-x64.exe')))
if (Test-Path -LiteralPath $BuildRoot) { throw "Build directory already exists; refusing to overwrite: $BuildRoot" }
if ($Output -ne $FixedTestOutput -and $Output -ne $VersionedTestOutput) {
    throw "Output must be the fixed test installer or the versioned installer for $($VersionInfo.version): $Output"
}
if ((Test-Path -LiteralPath $Output) -and $Output -ne $FixedTestOutput) {
    throw "Refusing to overwrite a versioned installer: $Output"
}
$BuildRoot = (New-Item -ItemType Directory -Path $BuildRoot).FullName
$env:PYTHONPYCACHEPREFIX = (New-Item -ItemType Directory -Force -Path (Join-Path $BuildRoot 'pycache')).FullName
$env:PYINSTALLER_CONFIG_DIR = (New-Item -ItemType Directory -Force -Path (Join-Path $BuildRoot 'pyinstaller-cache')).FullName
$env:TEMP = $env:TMP = (New-Item -ItemType Directory -Force -Path (Join-Path $BuildRoot 'tmp')).FullName
$Product = 'CCSLab Labcoding'
$Source = Join-Path (Split-Path $PSScriptRoot) 'app'
$Compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'

function Confirm-NativeSuccess([string]$Operation) {
    if ($LASTEXITCODE -ne 0) {
        throw "$Operation failed (exit $LASTEXITCODE)"
    }
}

if (!(Test-Path -LiteralPath $Compiler)) { throw '.NET Framework C# compiler is required.' }
if ($PyInstallerPath) { $env:PYTHONPATH = (Resolve-Path -LiteralPath $PyInstallerPath).Path }
& $Python -c "import platform, tkinter, markdown_it, urllib3; assert platform.machine().upper() == 'AMD64'"
Confirm-NativeSuccess 'Checking x64 Python, Tk, Markdown and HTTPS proxy support'

$Vendor = Join-Path $Source 'vendor'
$OpenCode = Join-Path $LocalWork 'vendor\opencode.exe'
if (!(Test-Path -LiteralPath $OpenCode)) {
    & $Python (Join-Path $PSScriptRoot 'Fetch-OpenCode.py')
    Confirm-NativeSuccess 'Verifying OpenCode distribution'
}
$OpenCodeHash = '488074BC6780C2EA7BE6B64DAB34C0D45BD03B30DEF13675CA7993889262C23E'
if ((Get-FileHash -LiteralPath $OpenCode -Algorithm SHA256).Hash -ne $OpenCodeHash) {
    throw 'The supplied OpenCode.exe is not the pinned Windows x64 baseline distribution.'
}

$Dist = Join-Path $BuildRoot 'dist'
$Assets = (New-Item -ItemType Directory -Force -Path (Join-Path $BuildRoot 'assets')).FullName
$Icon = Join-Path $Assets 'app.ico'
$IconPng = Join-Path $Assets 'app.png'
$IconBuilder = Join-Path $BuildRoot 'Build-Icon.exe'
& $Compiler /nologo /target:exe /r:System.Drawing.dll /r:System.Xml.dll "/out:$IconBuilder" (Join-Path $PSScriptRoot 'Build-Icon.cs')
Confirm-NativeSuccess 'Compiling PNG icon converter'
& $IconBuilder (Join-Path $Source 'assets\app.png') $Icon $IconPng
Confirm-NativeSuccess 'Rendering supplied application icon'
$Frozen = Join-Path $Dist $Product
$AnalysisOutput = Join-Path $BuildRoot 'pyinstaller'
$FreezeOptions = @(
    '--noconfirm', '--windowed', '--onedir', '--noupx', '--name', $Product,
    '--icon', $Icon, '--add-data', "$Icon;assets", '--add-data', "$IconPng;assets",
    '--add-data', "$(Join-Path $Source 'version.json');.",
    '--version-file', (Join-Path $BuildRoot 'version-resource.txt'),
    '--specpath', $BuildRoot, '--workpath', $AnalysisOutput, '--distpath', $Dist
)
$versionCore = $VersionInfo.version.Split('-')[0].Split('.')
$fileVersion = "{0}.{1}.{2}.{3}" -f [int]$versionCore[0], [int]$versionCore[1], [int]$versionCore[2], [int]$VersionInfo.release_sequence
$fileVersionTuple = ($fileVersion.Split('.') -join ', ')
$productVersion = $VersionInfo.version
@"
VSVersionInfo(
  ffi=FixedFileInfo(filevers=($fileVersionTuple), prodvers=($fileVersionTuple), mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[StringFileInfo([StringTable('040904B0', [StringStruct('CompanyName', 'CCSLab'), StringStruct('FileDescription', 'CCSLab Labcoding'), StringStruct('FileVersion', '$productVersion'), StringStruct('ProductName', 'CCSLab Labcoding'), StringStruct('ProductVersion', '$productVersion')])]), VarFileInfo([VarStruct('Translation', [1033, 1200])])]
)
"@ | Set-Content -LiteralPath (Join-Path $BuildRoot 'version-resource.txt') -Encoding utf8
& $Python -m PyInstaller @FreezeOptions (Join-Path $Source 'app.py')
Confirm-NativeSuccess 'Freezing desktop application'

$FrozenVendor = (New-Item -ItemType Directory -Force -Path (Join-Path $Frozen 'vendor')).FullName
Copy-Item -LiteralPath $OpenCode -Destination $FrozenVendor
Get-ChildItem -LiteralPath $Vendor -Filter 'LICENSE-*.txt' -File |
    Copy-Item -Destination $FrozenVendor
$PythonHome = & $Python -c "import sys; print(sys.base_prefix)"
foreach ($Name in @('LICENSE.txt', 'LICENSE_PYTHON.txt')) {
    $License = Join-Path $PythonHome $Name
    if (Test-Path -LiteralPath $License) {
        Copy-Item -LiteralPath $License -Destination (Join-Path $FrozenVendor 'LICENSE-Python.txt')
        break
    }
}

$SetupSource = Join-Path $PSScriptRoot 'Setup.cs'
$Uninstaller = Join-Path $Frozen 'Uninstall.exe'
$Payload = Join-Path $BuildRoot 'program.zip'
$IntermediateSetup = Join-Path $BuildRoot 'Setup.exe'
$CompilerOptions = @(
    '/nologo', '/target:winexe', '/optimize+', '/utf8output', '/codepage:65001',
    '/platform:x64', "/win32manifest:$(Join-Path $PSScriptRoot 'as-invoker.manifest')", "/win32icon:$Icon",
    '/r:System.dll', '/r:System.Core.dll', '/r:System.Drawing.dll',
    '/r:System.Windows.Forms.dll', '/r:System.IO.Compression.dll'
)
$AssemblyInfo = Join-Path $BuildRoot 'AssemblyVersion.cs'
@"
using System.Reflection;
[assembly: AssemblyVersion("$fileVersion")]
[assembly: AssemblyFileVersion("$fileVersion")]
[assembly: AssemblyInformationalVersion("$productVersion")]
"@ | Set-Content -LiteralPath $AssemblyInfo -Encoding utf8
& $Compiler @CompilerOptions /define:UNINSTALLER "/out:$Uninstaller" $AssemblyInfo $SetupSource
Confirm-NativeSuccess 'Compiling uninstall program'
& $Python (Join-Path $PSScriptRoot 'Create-Payload.py') $Frozen $Payload
Confirm-NativeSuccess 'Compressing application data'
& $Compiler @CompilerOptions "/resource:$Payload,ccslab-payload" "/out:$IntermediateSetup" $AssemblyInfo $SetupSource
Confirm-NativeSuccess 'Compiling installer'

New-Item -ItemType Directory -Force -Path (Split-Path $Output) | Out-Null
Copy-Item -LiteralPath $IntermediateSetup -Destination $Output -Force
Get-FileHash -LiteralPath $Output -Algorithm SHA256
