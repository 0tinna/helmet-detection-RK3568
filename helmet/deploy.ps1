param(
    [string]$Board = 'rk',
    [string]$RemoteRoot = '/userdata/helmet-validation/deployment'
)
$ErrorActionPreference = 'Stop'
function Invoke-Checked {
    param([string]$Program, [string[]]$Arguments)
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Program failed with exit code $LASTEXITCODE" }
}
try {
    if ($Board -notmatch '^[A-Za-z0-9][A-Za-z0-9.-]*$') {
        throw 'Board must be an SSH alias, hostname or IP without shell syntax.'
    }
    if ($RemoteRoot -notmatch '^/userdata/helmet-validation/[A-Za-z0-9._/-]+$' -or
        ($RemoteRoot -split '/') -contains '..') {
        throw 'RemoteRoot must stay below /userdata/helmet-validation.'
    }
    $target = if ($Board -eq 'rk') { 'rk' } else { "root@$Board" }
    $options = @('-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10')
    $projectRoot = Split-Path -Parent $PSScriptRoot
    Invoke-Checked ssh ($options + @($target,
        "mkdir -p $RemoteRoot/src/pc $RemoteRoot/src/legacy_tests $RemoteRoot/repo/helmet/pc $RemoteRoot/repo/helmet/tests $RemoteRoot/repo/tests $RemoteRoot/repo/docs/experiments/2026-11-14 $RemoteRoot/models $RemoteRoot/input $RemoteRoot/results"))
    $scripts = @(Get-ChildItem -LiteralPath $PSScriptRoot -Filter '*.py' -File | ForEach-Object { $_.FullName })
    Invoke-Checked scp ($options + $scripts + @("${target}:${RemoteRoot}/src/"))
    Invoke-Checked scp ($options + $scripts + @("${target}:${RemoteRoot}/repo/helmet/"))
    $pcScripts = @(Get-ChildItem -LiteralPath (Join-Path $PSScriptRoot 'pc') -Filter '*.py' -File | ForEach-Object { $_.FullName })
    Invoke-Checked scp ($options + $pcScripts + @("${target}:${RemoteRoot}/src/pc/"))
    Invoke-Checked scp ($options + $pcScripts + @("${target}:${RemoteRoot}/repo/helmet/pc/"))
    if (Test-Path -LiteralPath (Join-Path $projectRoot 'tests')) {
        $tests = @(Get-ChildItem -LiteralPath (Join-Path $projectRoot 'tests') -Filter '*.py' -File | ForEach-Object { $_.FullName })
        if ($tests.Count) { Invoke-Checked scp ($options + $tests + @("${target}:${RemoteRoot}/repo/tests/")) }
    }
    $legacyTests = @(Get-ChildItem -LiteralPath (Join-Path $PSScriptRoot 'tests') -Filter '*.py' -File | ForEach-Object { $_.FullName })
    Invoke-Checked scp ($options + $legacyTests + @("${target}:${RemoteRoot}/src/legacy_tests/"))
    Invoke-Checked scp ($options + $legacyTests + @("${target}:${RemoteRoot}/repo/helmet/tests/"))
    $verificationScript = Join-Path $projectRoot 'docs\experiments\2026-11-14\verify_runtime.py'
    Invoke-Checked scp ($options + @($verificationScript,
        "${target}:${RemoteRoot}/repo/docs/experiments/2026-11-14/"))
    Invoke-Checked scp ($options + @((Join-Path $projectRoot 'requirements-board.txt'),
        (Join-Path $projectRoot 'requirements-host.txt'), "${target}:${RemoteRoot}/repo/"))
    $models = @(Get-ChildItem -LiteralPath (Join-Path $PSScriptRoot 'models') -Filter '*.rknn' -File | ForEach-Object { $_.FullName })
    $baseline = Join-Path $projectRoot 'models\helmet-640-640.rknn'
    if (Test-Path -LiteralPath $baseline) { $models += $baseline }
    Invoke-Checked scp ($options + $models + @("${target}:${RemoteRoot}/models/"))
    Invoke-Checked scp ($options + @((Join-Path $projectRoot 'models\1.jpg'), "${target}:${RemoteRoot}/input/1.jpg"))
    Invoke-Checked ssh ($options + @($target,
        "cd $RemoteRoot/src && python3 -m compileall -q . && python3 helmet_realtime.py --help && cd $RemoteRoot/repo && python3 -m compileall -q helmet tests && python3 -m unittest discover -s tests -p test_sources.py -v && sha256sum $RemoteRoot/models/*.rknn"))
    Write-Host "Verified deployment: $RemoteRoot"
    Write-Host "Full regression tree: ssh $target 'cd $RemoteRoot/repo && python3 -m unittest discover -s tests -v'"
    Write-Host "Replay: ssh $target 'cd $RemoteRoot/src && python3 helmet_realtime.py --model $RemoteRoot/models/helmet_416x416.rknn --source $RemoteRoot/input --conf 0.25 --no-display --duration 10'"
    exit 0
}
catch {
    Write-Error -ErrorAction Continue $_.Exception.Message
    exit 1
}
