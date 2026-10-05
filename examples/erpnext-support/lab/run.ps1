[CmdletBinding()]
param(
    [ValidateSet('create','up','status','fault','restore','manual_recheck','down')]
    [string]$Action = 'status',
    [string]$Config,
    [ValidateSet('database_unavailable','cache_unavailable','api_unavailable')]
    [string]$Fault
)
$ErrorActionPreference = 'Stop'
$taskRepo = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..\..'))
$taskPython = Join-Path $taskRepo '.venv\Scripts\python.exe'
& $taskPython -c "import sys; assert sys.version_info[:2] == (3,12); assert sys.prefix != sys.base_prefix"
if ($LASTEXITCODE -ne 0) { throw 'Existing independent Python 3.12 environment required.' }
$taskArgs = @((Join-Path $PSScriptRoot 'lab.py'), $Action)
if ($Config) { $taskArgs += @('--config', $Config) }
if ($Fault) { $taskArgs += @('--fault', $Fault) }
& $taskPython @taskArgs
if ($LASTEXITCODE -ne 0) { throw 'Scoped ERP lab operation failed; no wider cleanup was attempted.' }
