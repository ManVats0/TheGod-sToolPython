# The God's Tool: connect this computer in one command (Windows PowerShell).
#   & ([scriptblock]::Create((irm https://api.thegodstool.com/download/install.ps1))) ABCD-EFGH
param([Parameter(Position=0)][string]$Code, [Parameter(Position=1)][string]$Server = "https://api.thegodstool.com")
$ErrorActionPreference = "Stop"
if (-not $Code) { Write-Host "Missing pairing code. In the app open Tunneling > One click and copy the command shown there."; exit 1 }
$Dir = if ($env:THEGODSTOOL_HOME) { $env:THEGODSTOOL_HOME } else { Join-Path $HOME ".thegodstool" }
New-Item -ItemType Directory -Force -Path $Dir | Out-Null

$py = $null
foreach ($c in @("py -3", "python", "python3")) {
  try { $v = & cmd /c "$c -c ""import sys;print(sys.version_info>=(3,8))"" 2>NUL"; if ($v -match "True") { $py = $c; break } } catch {}
}
if ($py) {
  Write-Host "Setting up the agent (one time, about a minute)..."
  $venv = Join-Path $Dir "venv"
  if (-not (Test-Path (Join-Path $venv "Scripts\python.exe"))) { & cmd /c "$py -m venv ""$venv""" }
  $vpy = Join-Path $venv "Scripts\python.exe"
  & $vpy -m pip install -q --disable-pip-version-check websockets aiomysql asyncpg motor aiopg
  Invoke-WebRequest -Uri "$Server/download/agent.py" -OutFile (Join-Path $Dir "agent.py")
  Set-Content -Path (Join-Path $Dir "start.cmd") -Value "@echo off`r`n""$vpy"" ""$Dir\agent.py"" %*"
  Write-Host "To start it again later, run: $Dir\start.cmd"
  & $vpy (Join-Path $Dir "agent.py") --pair $Code --server $Server
} else {
  $exe = Join-Path $Dir "datapro-agent.exe"
  Invoke-WebRequest -Uri "https://github.com/ManVats0/TheGod-sToolPython/releases/latest/download/datapro-agent.exe" -OutFile $exe
  Write-Host "To start it again later, run: $exe"
  & $exe --pair $Code --server $Server
}
