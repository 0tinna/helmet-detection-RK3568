param(
    [string]$Board = 'rk',
    [string]$RemoteRoot = '/userdata/helmet-app',
    [switch]$VendorIcons
)
$ErrorActionPreference = 'Stop'
if ($RemoteRoot -notmatch '^/userdata/[A-Za-z0-9_/-]+$' -or $RemoteRoot.Contains('..')) {
    throw 'Use an absolute /userdata path with ordinary directory names.'
}
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
& (Join-Path $repoRoot 'helmet\deploy.ps1') -Board $Board -RemoteRoot $RemoteRoot
if ($LASTEXITCODE -ne 0) { throw 'Checked runtime deployment failed' }
& ssh $Board "mkdir -p $RemoteRoot/src/app"
if ($LASTEXITCODE -ne 0) { throw 'Application directory creation failed' }
foreach ($name in @(
    '__init__.py',
    'alert_events.py',
    'alert_hub.py',
    'alert_viewer.py',
    'autostart.sh',
    'boot.py',
    'install.py',
    'lan.py',
    'launch.py',
    'resident.py',
    'service.py',
    'wifi.py'
)) {
    & scp (Join-Path $PSScriptRoot $name) "${Board}:$RemoteRoot/src/app/$name"
    if ($LASTEXITCODE -ne 0) { throw "Application copy failed: $name" }
}
foreach ($name in @('qml','icons')) {
    & scp -r (Join-Path $PSScriptRoot $name) "${Board}:$RemoteRoot/src/app/"
    if ($LASTEXITCODE -ne 0) { throw "Asset copy failed: $name" }
}
& ssh $Board "python3 -m compileall -q $RemoteRoot/src/app"
if ($LASTEXITCODE -ne 0) { throw 'Application compilation failed' }
& ssh $Board "chmod -R go-rwx $RemoteRoot/src/app; chmod -R u+rwX $RemoteRoot/src/app; chmod 700 $RemoteRoot/src/app/autostart.sh"
if ($LASTEXITCODE -ne 0) { throw 'Application permission hardening failed' }
$installArgs = if ($VendorIcons) { ' --vendor-icons' } else { '' }
& ssh $Board "python3 $RemoteRoot/src/app/install.py --root $RemoteRoot$installArgs"
if ($LASTEXITCODE -ne 0) { throw 'Application icon registration failed' }
Write-Output "Native application installed: $RemoteRoot"
Write-Output "Launch: python3 $RemoteRoot/src/app/launch.py --root $RemoteRoot"
