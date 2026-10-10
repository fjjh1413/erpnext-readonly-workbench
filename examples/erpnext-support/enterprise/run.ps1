param(
    [Parameter(Mandatory=$true)]
    [ValidateSet('Prepare','Start','Check','Stop','Down','Backup','Restore','Upgrade','BuildContext')]
    [string]$Action,
    [string]$Config,
    [ValidateSet('enterprise','test')][string]$Mode='enterprise',
    [string]$Image='holmes-erp-enterprise:unbuilt',
    [string]$Backup,
    [switch]$Monitoring,
    [switch]$UnavailableFixture,
    [switch]$DataOnly
)
$ErrorActionPreference='Stop'
$taskRepo = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..\..'))
$taskPython = Join-Path $taskRepo '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $taskPython -PathType Leaf)) { throw '请先准备仓库独立 Python 3.12 环境，不覆盖现有环境。' }
$taskVersion = & $taskPython -c 'import sys; print(".".join(map(str,sys.version_info[:2])))'
if ($taskVersion -ne '3.12') { throw '此脚本要求独立 Python 3.12 环境。' }
if ($Action -eq 'BuildContext') {
    & $taskPython (Join-Path $PSScriptRoot 'build.py')
} else {
    $taskMap=@{Prepare='prepare';Start='up';Check='check';Stop='stop';Down='down';Backup='backup';Restore='restore';Upgrade='upgrade'}
    $taskArgs=@((Join-Path $PSScriptRoot 'enterprise.py'),$taskMap[$Action])
    if ($Action -eq 'Prepare') { $taskArgs+=@('--mode',$Mode,'--image',$Image) }
    else {
        if (-not $Config) { throw '必须指定本模块新建项目的 config.json。' }
        $taskArgs+=@('--config',$Config)
    }
    if ($Monitoring) { $taskArgs+='--monitoring' }
    if ($UnavailableFixture) { $taskArgs+='--unavailable-fixture' }
    if ($DataOnly) { $taskArgs+='--data-only' }
    if ($Action -eq 'Upgrade') { $taskArgs+=@('--image',$Image) }
    if ($Action -eq 'Restore') {
        if (-not $Backup) { throw '恢复需要显式选择本模块私有目录中实际备份文件。' }
        $taskArgs+=@('--backup',$Backup)
    }
    & $taskPython @taskArgs
}
if ($LASTEXITCODE -ne 0) { throw '操作没有完成，请检查新项目配置和实际错误。未接管其他项目。' }
