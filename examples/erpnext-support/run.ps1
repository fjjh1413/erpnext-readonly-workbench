param(
    [ValidateSet('Init','Start','Check','Stop','Backup')][string]$Action = 'Check',
    [string]$ErpUrl,
    [string]$SettingsPath,
    [string]$OutputPath
)
$ErrorActionPreference = 'Stop'
$taskRepo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$taskPython = Join-Path $taskRepo '.venv\Scripts\python.exe'
if (-not $SettingsPath) { $SettingsPath = Join-Path $PSScriptRoot '.runtime\workbench\settings.json' }
$SettingsPath = [IO.Path]::GetFullPath($SettingsPath)
$taskRuntime = Split-Path -Parent $SettingsPath
if (-not (Test-Path -LiteralPath $taskPython -PathType Leaf)) { throw '先按README建立独立Python3.12环境，不使用全局Python安装依赖。' }
$taskPythonVersion = (& $taskPython -c 'import sys; print(str(sys.version_info.major)+"."+str(sys.version_info.minor))').Trim()
if ($LASTEXITCODE -ne 0 -or $taskPythonVersion -ne '3.12') { throw '该工作台要求独立Python3.12环境；保留现有环境，请先检查而不是覆盖。' }
if ($SettingsPath.Contains('"')) { throw '配置路径不能包含双引号。' }
Push-Location $taskRepo
try {
    switch ($Action) {
        'Init' {
            if (-not $ErpUrl) { throw 'Init需要明确的-ErpUrl授权ERP地址。' }
            & $taskPython -m holmes.erpnext_support init --settings $SettingsPath --erp-url $ErpUrl
            if ($LASTEXITCODE -ne 0) { throw '初始化未完成；现有目录不会覆盖。' }
        }
        'Start' {
            if (-not (Test-Path -LiteralPath $SettingsPath)) { throw '先运行Init并配置受控凭据。' }
            if (Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue) { throw '8765已被占用，先检查现有服务；不自动停止。' }
            if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'frontend\dist\index.html'))) {
                Push-Location (Join-Path $PSScriptRoot 'frontend')
                try {
                    & npm ci
                    if ($LASTEXITCODE -ne 0) { throw '前端依赖安装失败。' }
                    & npm run build
                    if ($LASTEXITCODE -ne 0) { throw '前端构建失败。' }
                } finally { Pop-Location }
            }
            $taskStamp = [guid]::NewGuid().ToString('N')
            $taskProcess = Start-Process -FilePath $taskPython -ArgumentList @('-m','holmes.erpnext_support','serve','--settings',('"'+$SettingsPath+'"')) -WorkingDirectory $taskRepo -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $taskRuntime ('server-'+$taskStamp+'.log')) -RedirectStandardError (Join-Path $taskRuntime ('server-'+$taskStamp+'-error.log'))
            @{ pid=$taskProcess.Id; settings=$SettingsPath; module='holmes.erpnext_support'; started_at=(Get-Date).ToString('o') } | ConvertTo-Json | Out-File -LiteralPath (Join-Path $taskRuntime ('server-'+$taskStamp+'.json')) -Encoding utf8 -NoClobber
            Write-Output '启动已提交，首次导入依赖可能约一分钟；运行Check查看实际就绪状态。地址：http://127.0.0.1:8765'
        }
        'Check' {
            & $taskPython -m holmes.erpnext_support check --settings $SettingsPath
            if ($LASTEXITCODE -ne 0) { throw '配置检查失败。' }
            Invoke-RestMethod -Uri 'http://127.0.0.1:8765/health' -TimeoutSec 5 | ConvertTo-Json
            & docker version --format '{{.Server.Version}}'
            if ($LASTEXITCODE -ne 0) { Write-Warning 'Docker不可用；ERP实验和运行状态采集不可用，工作台文件保留。' }
        }
        'Stop' {
            $taskManifests = Get-ChildItem -LiteralPath $taskRuntime -Filter 'server-*.json' | Sort-Object LastWriteTime -Descending
            foreach ($taskManifest in $taskManifests) {
                $taskInfo = Get-Content -LiteralPath $taskManifest.FullName -Raw | ConvertFrom-Json
                if ($taskInfo.settings -and $taskInfo.settings -ne $SettingsPath) { continue }
                $taskProcess = Get-CimInstance Win32_Process -Filter ('ProcessId='+[int]$taskInfo.pid)
                $taskOwnedInterpreter = $taskProcess -and $taskProcess.ExecutablePath -eq $taskPython
                if ($taskProcess -and -not $taskOwnedInterpreter) {
                    # Windows venv launcher can start the base Python as a child.
                    # Verify its live parent, instead of accepting any python.exe.
                    $taskParent = Get-CimInstance Win32_Process -Filter ('ProcessId='+[int]$taskProcess.ParentProcessId)
                    $taskOwnedInterpreter = $taskParent -and $taskParent.ExecutablePath -eq $taskPython -and $taskParent.CommandLine -match ' -m holmes\.erpnext_support serve(?: |$)'
                }
                $taskCorrectSettings = $taskProcess -and (($taskProcess.CommandLine -notmatch '(?:^|\s)--settings(?:\s|=)' -and $SettingsPath -eq (Join-Path $PSScriptRoot '.runtime\workbench\settings.json')) -or $taskProcess.CommandLine.Contains($SettingsPath))
                if ($taskProcess -and $taskOwnedInterpreter -and $taskCorrectSettings -and $taskProcess.CommandLine -match ' -m holmes\.erpnext_support serve(?: |$)') {
                    Stop-Process -Id $taskProcess.ProcessId
                    Write-Output ('已停止本工作台进程 '+$taskProcess.ProcessId)
                }
            }
        }
        'Backup' {
            if (-not $OutputPath) { throw 'Backup需要全新-OutputPath，不覆盖已有备份。' }
            & $taskPython -m holmes.erpnext_support backup --settings $SettingsPath --output $OutputPath
            if ($LASTEXITCODE -ne 0) { throw '备份未完成。' }
        }
    }
} finally { Pop-Location }
