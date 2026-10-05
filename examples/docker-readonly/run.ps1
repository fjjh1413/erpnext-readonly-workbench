[CmdletBinding()]
param(
    [ValidateSet('create', 'up', 'fault', 'restore', 'status', 'down', 'evaluate')]
    [string]$Action = 'evaluate',
    [string]$Config,
    [string]$Output,
    [ValidateSet('database_unavailable', 'cache_connection_error', 'service_configuration_error')]
    [string]$Fault
)

$ErrorActionPreference = 'Stop'
$taskRepo = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$taskPython = Join-Path $taskRepo '.venv\Scripts\python.exe'
if ((Test-Path -LiteralPath (Join-Path $taskRepo '.venv')) -and
    ((Get-Item -LiteralPath (Join-Path $taskRepo '.venv') -Force).Attributes -band [System.IO.FileAttributes]::ReparsePoint)) {
    throw 'The repository virtual environment must not be a symlink or junction.'
}
if (-not (Test-Path -LiteralPath $taskPython)) {
    throw 'Run bootstrap.ps1 first to create the independent Python 3.12 environment.'
}
& $taskPython -c "import sys; assert sys.version_info[:2] == (3, 12); assert sys.prefix != sys.base_prefix"
if ($LASTEXITCODE -ne 0) { throw 'An independent Python 3.12 environment is required.' }

if ($Action -eq 'evaluate') {
    # evaluate.py creates and closes its own fresh isolated lab.
    if (-not $Output) {
        $taskSuffix = [Guid]::NewGuid().ToString('N').Substring(0, 8)
        $taskTimestamp = (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')
        $Output = Join-Path $PSScriptRoot "reports\run-$taskTimestamp-$taskSuffix"
    }
    & $taskPython (Join-Path $PSScriptRoot 'evaluate.py') --output $Output
} else {
    $taskArguments = @((Join-Path $PSScriptRoot 'lab.py'), $Action)
    if ($Config) { $taskArguments += @('--config', $Config) }
    if ($Fault) { $taskArguments += @('--fault', $Fault) }
    & $taskPython @taskArguments
}
if ($LASTEXITCODE -ne 0) { throw "The '$Action' operation failed. Review the reported failure; no wider cleanup was attempted." }
