param([ValidateSet('setup','build','test','run','freeze')][string]$Task='run', [int]$Port=8091, [switch]$Reload)
$ErrorActionPreference='Stop'
Set-Location -LiteralPath $PSScriptRoot
$pythonPath=Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
function Check-Exit { if ($LASTEXITCODE -ne 0) { throw "命令执行失败，退出码 $LASTEXITCODE" } }
switch ($Task) {
  'setup' {
    if (!(Test-Path -LiteralPath $pythonPath)) { python -m venv .venv; Check-Exit }
    # 使用系统新版 pip 管理虚拟环境，兼容本机 Python 3.10 附带的旧 pip。
    $pipArgs=@('-m','pip','--python',$pythonPath,'install')
    $proxyUrl=[Environment]::GetEnvironmentVariable('HTTPS_PROXY')
    if (!$proxyUrl) {
      $proxyConfig=Get-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings' -ErrorAction SilentlyContinue
      if ($proxyConfig.ProxyEnable -eq 1 -and $proxyConfig.ProxyServer -match '127\.0\.0\.1:7890') { $proxyUrl='http://127.0.0.1:7890' }
    }
    if ($proxyUrl) { $pipArgs+=@('--proxy',$proxyUrl) }
    & python @pipArgs -r requirements.lock; Check-Exit
    & python @pipArgs --no-build-isolation -e '.[test]'; Check-Exit
    Push-Location frontend
    try { npm ci; Check-Exit; npm run build; Check-Exit } finally { Pop-Location }
  }
  'build' { Push-Location frontend; try { npm run build; Check-Exit } finally { Pop-Location } }
  'test' { & $pythonPath -m pytest -q; Check-Exit }
  'freeze' { & $pythonPath -m pip freeze | Set-Content -Encoding utf8 requirements.lock; Check-Exit }
  'run' { if (!(Test-Path -LiteralPath $pythonPath)) { throw '请先执行 .\dev.ps1 -Task setup' }; $serverArgs=@('-m','uvicorn','procurepilot.api:create_app','--factory','--host','127.0.0.1','--port',"$Port"); if ($Reload) { $serverArgs+=@('--reload','--reload-dir',(Join-Path $PSScriptRoot 'procurepilot')) }; & $pythonPath @serverArgs; Check-Exit }
}
