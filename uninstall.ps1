param(
    [Parameter(Mandatory = $false, Position = 0)]
    [string]$InstallDir = "",

    [switch]$RemoveConfig
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($InstallDir)) {
    $resolvedInstallDir = Join-Path $env:LOCALAPPDATA "OpenCANNBotProxyCenter"
}
else {
    $expandedInstallDir = [Environment]::ExpandEnvironmentVariables($InstallDir)
    if ([System.IO.Path]::IsPathRooted($expandedInstallDir)) {
        $resolvedInstallDir = [System.IO.Path]::GetFullPath($expandedInstallDir)
    }
    else {
        $resolvedInstallDir = [System.IO.Path]::GetFullPath(
            (Join-Path (Get-Location).Path $expandedInstallDir)
        )
    }
}

$centerScript = Join-Path $resolvedInstallDir "opencannbot_proxy_center.py"
if (Test-Path -LiteralPath $centerScript -PathType Leaf) {
    $py = Get-Command "py.exe" -ErrorAction SilentlyContinue
    if ($null -ne $py) {
        & $py.Source -3 $centerScript --stop-all 2>$null
    }
    else {
        $python = Get-Command "python.exe" -ErrorAction SilentlyContinue
        if ($null -ne $python) {
            & $python.Source $centerScript --stop-all 2>$null
        }
    }
}

Get-CimInstance Win32_Process |
    Where-Object {
        $_.CommandLine -like "*opencannbot_proxy_center.py*" -or
        $_.CommandLine -like "*opencannbot_instance_proxy.py*"
    } |
    ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }

Start-Sleep -Milliseconds 350

if (Test-Path -LiteralPath $resolvedInstallDir) {
    Remove-Item -LiteralPath $resolvedInstallDir -Recurse -Force
    Write-Host "Removed program files: $resolvedInstallDir" -ForegroundColor Green
}
else {
    Write-Host "Program directory was not found: $resolvedInstallDir"
}

if ($RemoveConfig) {
    $dataDir = Join-Path $HOME ".cannbot\proxy-center"
    if (Test-Path -LiteralPath $dataDir) {
        Remove-Item -LiteralPath $dataDir -Recurse -Force
        Write-Host "Removed proxy-center configuration, VK files, PIDs, and logs: $dataDir"
    }
}
else {
    Write-Host "Proxy-center data was kept under $HOME\.cannbot\proxy-center"
    Write-Host "Add -RemoveConfig to delete instance VK files and logs too."
}
