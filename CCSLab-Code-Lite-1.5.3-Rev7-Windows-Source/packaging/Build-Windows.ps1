param(
    [string]$Output = (Join-Path (Split-Path (Split-Path $PSScriptRoot)) '.work\releases\CCSLab-Code-Lite-1.5.3-Rev11-Setup-x64.exe'),
    [string]$Python = 'python',
    [string]$PyInstallerPath = '',
    [string]$BuildRoot = (Join-Path (Split-Path (Split-Path $PSScriptRoot)) '.work\build\Rev11')
)

$ErrorActionPreference = 'Stop'
$LocalWork = Join-Path (Split-Path (Split-Path $PSScriptRoot)) '.work'
$env:PYTHONPYCACHEPREFIX = (New-Item -ItemType Directory -Force -Path (Join-Path $LocalWork 'pycache')).FullName
$env:PYINSTALLER_CONFIG_DIR = (New-Item -ItemType Directory -Force -Path (Join-Path $LocalWork 'pyinstaller-cache')).FullName
$env:TEMP = $env:TMP = (New-Item -ItemType Directory -Force -Path (Join-Path $LocalWork 'tmp')).FullName
$Product = 'CCSLab Code Lite'
$Source = Join-Path (Split-Path $PSScriptRoot) 'app'
$Compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
$BuildRoot = (New-Item -ItemType Directory -Force -Path $BuildRoot).FullName
$Output = [System.IO.Path]::GetFullPath($Output)

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
$Assets = (New-Item -ItemType Directory -Force -Path (Join-Path $LocalWork 'assets\Rev10')).FullName
$Icon = Join-Path $Assets 'app.ico'
$IconPng = Join-Path $Assets 'app.png'
$IconBuilder = Join-Path $BuildRoot 'Build-Icon.exe'
& $Compiler /nologo /target:exe /r:System.Drawing.dll /r:System.Xml.dll "/out:$IconBuilder" (Join-Path $PSScriptRoot 'Build-Icon.cs')
Confirm-NativeSuccess 'Compiling vector icon renderer'
& $IconBuilder (Join-Path $Source 'assets\logo.svg') $Icon $IconPng
Confirm-NativeSuccess 'Rendering mint monogram icons'
$Frozen = Join-Path $Dist $Product
$AnalysisOutput = Join-Path $BuildRoot 'pyinstaller'
$FreezeOptions = @(
    '--noconfirm', '--windowed', '--onedir', '--noupx', '--name', $Product,
    '--icon', $Icon, '--add-data', "$Icon;assets", '--add-data', "$IconPng;assets",
    '--specpath', $BuildRoot, '--workpath', $AnalysisOutput, '--distpath', $Dist
)
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
& $Compiler @CompilerOptions /define:UNINSTALLER "/out:$Uninstaller" $SetupSource
Confirm-NativeSuccess 'Compiling uninstall program'
& $Python (Join-Path $PSScriptRoot 'Create-Payload.py') $Frozen $Payload
Confirm-NativeSuccess 'Compressing application data'
& $Compiler @CompilerOptions "/resource:$Payload,ccslab-payload" "/out:$IntermediateSetup" $SetupSource
Confirm-NativeSuccess 'Compiling installer'

New-Item -ItemType Directory -Force -Path (Split-Path $Output) | Out-Null
Copy-Item -LiteralPath $IntermediateSetup -Destination $Output -Force
Get-FileHash -LiteralPath $Output -Algorithm SHA256
