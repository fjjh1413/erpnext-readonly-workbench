[CmdletBinding()]
param(
    [ValidateSet('TestedWindowsSnapshot', 'UpstreamLock')]
    [string]$Dependencies = 'TestedWindowsSnapshot'
)

$ErrorActionPreference = 'Stop'
$taskRepo = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$taskVenv = Join-Path $taskRepo '.venv'
$taskPython = Join-Path $taskVenv 'Scripts\python.exe'

if (Test-Path -LiteralPath $taskVenv) {
    $taskVenvItem = Get-Item -LiteralPath $taskVenv -Force
    if ($taskVenvItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
        throw 'The repository virtual environment must not be a symlink or junction.'
    }
    if (-not (Test-Path -LiteralPath $taskPython)) {
        throw "Existing .venv is not a usable virtual environment. No files were overwritten: $taskVenv"
    }
    & $taskPython -c "import sys; assert sys.version_info[:2] == (3, 12), 'Existing .venv is not Python 3.12'; assert sys.prefix != sys.base_prefix, 'Independent virtual environment required'"
    if ($LASTEXITCODE -ne 0) { throw 'Existing environment validation failed; no environment was replaced.' }
} else {
    & py -3.12 -c "import sys; assert sys.version_info[:2] == (3, 12)"
    if ($LASTEXITCODE -ne 0) { throw 'Install Python 3.12 before running this script.' }
    & py -3.12 -m venv $taskVenv
    if ($LASTEXITCODE -ne 0) { throw 'Could not create an independent Python 3.12 environment.' }
}

Push-Location -LiteralPath $taskRepo
try {
    if ($Dependencies -eq 'UpstreamLock') {
        # Opt-in upstream environment: preserve Poetry 1.8's lockfile format.
        & $taskPython -m pip install 'poetry==1.8.5'
        if ($LASTEXITCODE -ne 0) { throw 'Poetry installation failed.' }
        $taskPreviousVirtualEnv = $env:VIRTUAL_ENV
        $env:VIRTUAL_ENV = $taskVenv
        try {
            & $taskPython -m poetry install --with dev --no-interaction
            if ($LASTEXITCODE -ne 0) { throw 'Locked project dependency installation failed.' }
        } finally {
            $env:VIRTUAL_ENV = $taskPreviousVirtualEnv
        }
    } else {
        # This snapshot matches the actual Windows Python 3.12 verification.
        # Neither installation path regenerates or rewrites poetry.lock.
        & $taskPython -m pip install -r (Join-Path $PSScriptRoot 'requirements-python312-windows.txt')
        if ($LASTEXITCODE -ne 0) { throw 'Tested Windows dependency installation failed.' }
        & $taskPython -m pip install -e $taskRepo --no-deps
        if ($LASTEXITCODE -ne 0) { throw 'Editable project installation failed.' }
        & $taskPython -m pip check
        if ($LASTEXITCODE -ne 0) { throw 'Dependency consistency validation failed.' }
    }
} finally {
    Pop-Location
}
Write-Host "Python 3.12 environment ready: $taskPython"
