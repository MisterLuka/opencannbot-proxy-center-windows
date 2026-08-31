param(
    [Parameter(Mandatory = $false, Position = 0)]
    [string]$RepoDir = "",

    [Parameter(Mandatory = $false, Position = 1)]
    [string]$InstallDir = "",

    [Parameter(Mandatory = $false)]
    [Alias("Force")]
    [switch]$Overwrite
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoGitUrl = "https://github.com/BadFatCat0919/opencannbot.git"
$RepoZipUrl = "https://github.com/BadFatCat0919/opencannbot/archive/refs/heads/main.zip"
$RepoRequiredFile = "cannbot-claude-proxy.py"

function Find-Python {
    $pyLauncher = Get-Command "py.exe" -ErrorAction SilentlyContinue
    if ($null -ne $pyLauncher) {
        return [PSCustomObject]@{ Exe = $pyLauncher.Source; Prefix = @("-3") }
    }

    $pythonLauncher = Get-Command "python.exe" -ErrorAction SilentlyContinue
    if ($null -ne $pythonLauncher) {
        return [PSCustomObject]@{ Exe = $pythonLauncher.Source; Prefix = @() }
    }

    throw "Python was not found. Install Python 3.8 or newer and enable Add Python to PATH."
}

function Resolve-FullPath {
    param([Parameter(Mandatory = $true)][string]$PathText)

    $expanded = [Environment]::ExpandEnvironmentVariables($PathText)
    if ([System.IO.Path]::IsPathRooted($expanded)) {
        return [System.IO.Path]::GetFullPath($expanded)
    }
    return [System.IO.Path]::GetFullPath((Join-Path (Get-Location).Path $expanded))
}

function Find-ExistingInstallDirectories {
    param([Parameter(Mandatory = $true)][string]$ParentDir)

    $found = @()
    $directCenter = Join-Path $ParentDir "opencannbot_proxy_center.py"
    if (Test-Path -LiteralPath $directCenter -PathType Leaf) {
        $found += $ParentDir
    }
    if (Test-Path -LiteralPath $ParentDir -PathType Container) {
        foreach ($child in @(Get-ChildItem -LiteralPath $ParentDir -Directory -Force -ErrorAction SilentlyContinue)) {
            $childCenter = Join-Path $child.FullName "opencannbot_proxy_center.py"
            if (Test-Path -LiteralPath $childCenter -PathType Leaf) {
                $found += $child.FullName
            }
        }
    }
    return @($found | Select-Object -Unique)
}

function Test-ProxyRepo {
    param([Parameter(Mandatory = $true)][string]$Path)

    return (Test-Path -LiteralPath (Join-Path $Path $RepoRequiredFile) -PathType Leaf)
}

function Get-ProxyRepo {
    param([Parameter(Mandatory = $true)][string]$TargetDir)

    if (Test-ProxyRepo $TargetDir) {
        Write-Host "Existing OpenCANNBot repository found; reusing: $TargetDir" -ForegroundColor Yellow
        return
    }
    if (Test-Path -LiteralPath $TargetDir) {
        if (@(Get-ChildItem -LiteralPath $TargetDir -Force -ErrorAction SilentlyContinue).Count -gt 0) {
            throw "The repository directory exists but is not a valid OpenCANNBot checkout (missing $RepoRequiredFile): $TargetDir`nDelete it or pass -RepoDir with a valid path."
        }
        Remove-Item -LiteralPath $TargetDir -Force
    }

    $git = Get-Command "git.exe" -ErrorAction SilentlyContinue
    if ($null -ne $git) {
        Write-Host "Downloading the OpenCANNBot repository with git clone..." -ForegroundColor Yellow
        & $git.Source clone --depth 1 $RepoGitUrl $TargetDir
        if ($LASTEXITCODE -ne 0) {
            throw "git clone failed. Download $RepoGitUrl manually and pass the path via -RepoDir."
        }
    }
    else {
        Write-Host "git was not found; downloading the OpenCANNBot repository as a zip archive..." -ForegroundColor Yellow
        $savedProgress = $ProgressPreference
        $ProgressPreference = "SilentlyContinue"
        $zipFile = Join-Path ([System.IO.Path]::GetTempPath()) "opencannbot-main.zip"
        $extractRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("opencannbot-extract-" + [guid]::NewGuid().ToString("N"))
        try {
            [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
            Invoke-WebRequest -Uri $RepoZipUrl -OutFile $zipFile -UseBasicParsing
            Expand-Archive -LiteralPath $zipFile -DestinationPath $extractRoot -Force
            $extracted = @(Get-ChildItem -LiteralPath $extractRoot -Directory -ErrorAction SilentlyContinue) | Select-Object -First 1
            if ($null -eq $extracted) {
                throw "The downloaded archive did not contain the expected repository folder."
            }
            Move-Item -LiteralPath $extracted.FullName -Destination $TargetDir
        }
        finally {
            $ProgressPreference = $savedProgress
            Remove-Item -LiteralPath $zipFile -Force -ErrorAction SilentlyContinue
            Remove-Item -LiteralPath $extractRoot -Recurse -Force -ErrorAction SilentlyContinue
        }
    }

    if (-not (Test-ProxyRepo $TargetDir)) {
        throw "The downloaded repository is invalid (missing $RepoRequiredFile): $TargetDir"
    }
}

function Stop-ExistingInstall {
    param([Parameter(Mandatory = $true)][string]$CenterScript)

    if (-not (Test-Path -LiteralPath $CenterScript -PathType Leaf)) {
        return
    }

    Write-Host "Stopping the existing proxy center before overwrite..." -ForegroundColor Yellow

    $needle = $CenterScript.ToLowerInvariant()
    $processes = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object {
            $line = [string]$_.CommandLine
            $line -and $line.ToLowerInvariant().Contains($needle) -and $_.ProcessId -ne $PID
        }
    foreach ($process in @($processes)) {
        Stop-Process -Id ([int]$process.ProcessId) -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Milliseconds 350
}

if ([string]::IsNullOrWhiteSpace($InstallDir)) {
    $requestedInstallDir = Join-Path $env:LOCALAPPDATA "OpenCANNBotProxyCenter"
}
else {
    $requestedInstallDir = Resolve-FullPath $InstallDir
}

$existingInstallDirs = @(Find-ExistingInstallDirectories $requestedInstallDir)
if ($Overwrite -and $existingInstallDirs.Count -gt 1) {
    $foundPaths = $existingInstallDirs -join [Environment]::NewLine
    throw "Multiple existing installations were found. Specify the exact installation directory with -InstallDir:`n$foundPaths"
}
elseif ($Overwrite -and $existingInstallDirs.Count -eq 1 -and $existingInstallDirs[0] -ne $requestedInstallDir) {
    $resolvedInstallDir = $existingInstallDirs[0]
    Write-Host "Found existing installation under the requested directory; overwriting: $resolvedInstallDir" -ForegroundColor Yellow
}
else {
    $resolvedInstallDir = $requestedInstallDir
}

$python = Find-Python
$versionArgs = @()
$versionArgs += $python.Prefix
$versionArgs += @("-c", "import sys; assert sys.version_info >= (3, 8), 'Python 3.8 or newer is required'")
& $python.Exe @versionArgs
if ($LASTEXITCODE -ne 0) {
    throw "Python 3.8 or newer is required."
}

$resolveArgs = @()
$resolveArgs += $python.Prefix
$resolveArgs += @("-c", "import sys; print(sys.executable)")
$resolvedPythonExe = (& $python.Exe @resolveArgs | Select-Object -First 1).Trim()
if ([string]::IsNullOrWhiteSpace($resolvedPythonExe)) {
    throw "Could not resolve the Python executable."
}

$pythonwExe = Join-Path (Split-Path -Parent $resolvedPythonExe) "pythonw.exe"
if (-not (Test-Path -LiteralPath $pythonwExe -PathType Leaf)) {
    $pythonwExe = $resolvedPythonExe
}

$centerScript = Join-Path $resolvedInstallDir "opencannbot_proxy_center.py"
if ($Overwrite) {
    Stop-ExistingInstall $centerScript
}

New-Item -ItemType Directory -Force -Path $resolvedInstallDir | Out-Null

if (-not [string]::IsNullOrWhiteSpace($RepoDir)) {
    try {
        $resolvedRepo = (Resolve-Path -LiteralPath $RepoDir -ErrorAction Stop).Path
    }
    catch {
        throw "The repository path does not exist: $RepoDir"
    }
    if (-not (Test-ProxyRepo $resolvedRepo)) {
        throw "Invalid OpenCANNBot repository. Missing file: $(Join-Path $resolvedRepo $RepoRequiredFile)"
    }
}
else {
    $resolvedRepo = Join-Path $resolvedInstallDir "opencannbot"
    Write-Host ""
    Write-Host "No -RepoDir was given; preparing the OpenCANNBot repository at:" -ForegroundColor Yellow
    Write-Host "  $resolvedRepo"
    Get-ProxyRepo -TargetDir $resolvedRepo
}

$files = @(
    "opencannbot_proxy_center.py",
    "opencannbot_instance_proxy.py",
    "proxy_center.css",
    "dashboard.js",
    "job.js",
    "edit.js"
)

foreach ($file in $files) {
    $source = Join-Path $PSScriptRoot $file
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
        throw "Missing installer file: $source"
    }
    $target = Join-Path $resolvedInstallDir $file
    $sourceFull = [System.IO.Path]::GetFullPath($source)
    $targetFull = [System.IO.Path]::GetFullPath($target)
    if (-not [string]::Equals($sourceFull, $targetFull, [System.StringComparison]::OrdinalIgnoreCase)) {
        Copy-Item -LiteralPath $source -Destination $target -Force
    }
}

if ($Overwrite) {
    Write-Host "Stopping managed proxy instances after replacing the program files..." -ForegroundColor Yellow
    $stopArgs = @()
    $stopArgs += $python.Prefix
    $stopArgs += @($centerScript, "--stop-all")
    & $python.Exe @stopArgs 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "Could not stop all managed proxy instances automatically. Check their status before starting the updated center."
    }
}

$initArgs = @()
$initArgs += $python.Prefix
$initArgs += @($centerScript, "--repo-dir", $resolvedRepo, "--init-only")
& $python.Exe @initArgs
if ($LASTEXITCODE -ne 0) {
    throw "Proxy center initialization failed."
}

$pythonwEscaped = $pythonwExe.Replace('"', '""')
$centerEscaped = $centerScript.Replace('"', '""')
$vbsText = 'Set shell = CreateObject("WScript.Shell")' + "`r`n" +
           'command = """" & "' + $pythonwEscaped + '" & """ """ & "' + $centerEscaped + '" & """"' + "`r`n" +
           'shell.Run command, 0, False' + "`r`n" +
           'WScript.Sleep 900' + "`r`n" +
           'shell.Run "http://127.0.0.1:8788", 1, False' + "`r`n"

$startVbs = Join-Path $resolvedInstallDir "start-proxy-center.vbs"
[System.IO.File]::WriteAllText($startVbs, $vbsText, [System.Text.Encoding]::ASCII)
[System.IO.File]::WriteAllText((Join-Path $resolvedInstallDir "start-panel.vbs"), $vbsText, [System.Text.Encoding]::ASCII)

$diagnosticCmd = Join-Path $resolvedInstallDir "proxy-center.cmd"
$cmdText = "@echo off`r`n" +
           '"' + $resolvedPythonExe + '" "' + $centerScript + '" %*' + "`r`n" +
           "exit /b %ERRORLEVEL%`r`n"
[System.IO.File]::WriteAllText($diagnosticCmd, $cmdText, [System.Text.Encoding]::ASCII)

$startCmd = Join-Path $resolvedInstallDir "start-proxy-center.cmd"
$startCmdText = "@echo off`r`n" +
                'wscript.exe //nologo "' + $startVbs + '"' + "`r`n"
[System.IO.File]::WriteAllText($startCmd, $startCmdText, [System.Text.Encoding]::ASCII)

Write-Host ""
Write-Host "Installation completed." -ForegroundColor Green
Write-Host "Repository: $resolvedRepo"
Write-Host "Program directory: $resolvedInstallDir"
Write-Host ""
Write-Host "Silent start:"
Write-Host "  $startVbs"
Write-Host ""
Write-Host "Open:"
Write-Host "  http://127.0.0.1:8788"
