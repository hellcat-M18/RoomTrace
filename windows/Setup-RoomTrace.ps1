$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$processorRoot = Join-Path $projectRoot "processor"
$venvRoot = Join-Path $processorRoot ".venv"
$venvPython = Join-Path $venvRoot "Scripts\python.exe"
$markerPath = Join-Path $PSScriptRoot ".roomtrace-installed"
$setupVersion = "roomtrace-0.3"

$script:PythonMode = $null
$script:PythonLauncher = $null
$script:PythonExe = $null

function Test-PythonCommand {
    try {
        if ($script:PythonMode -eq "launcher") {
            & $script:PythonLauncher -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" 2>$null
        } else {
            & $script:PythonExe -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" 2>$null
        }
        return $LASTEXITCODE -eq 0
    } catch {
        return $false
    }
}

function Set-PythonCommand {
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($launcher) {
        $script:PythonMode = "launcher"
        $script:PythonLauncher = $launcher.Source
        if (Test-PythonCommand) {
            return $true
        }
    }

    $python = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($python) {
        $script:PythonMode = "direct"
        $script:PythonExe = $python.Source
        if (Test-PythonCommand) {
            return $true
        }
    }

    $roots = @()
    if ($env:LOCALAPPDATA) {
        $roots += Join-Path $env:LOCALAPPDATA "Programs\Python"
    }
    if ($env:ProgramFiles) {
        $roots += Join-Path $env:ProgramFiles "Python"
    }
    foreach ($root in $roots) {
        if (!(Test-Path $root)) {
            continue
        }
        $candidate = Get-ChildItem -Path $root -Filter python.exe -File -Recurse -ErrorAction SilentlyContinue |
            Sort-Object FullName -Descending |
            Select-Object -First 1
        if ($candidate) {
            $script:PythonMode = "direct"
            $script:PythonExe = $candidate.FullName
            if (Test-PythonCommand) {
                return $true
            }
        }
    }
    return $false
}

function Invoke-SystemPython([string[]]$Arguments) {
    if ($script:PythonMode -eq "launcher") {
        & $script:PythonLauncher -3 @Arguments
    } else {
        & $script:PythonExe @Arguments
    }
    if ($LASTEXITCODE -ne 0) {
        throw "Python command failed with exit code $LASTEXITCODE"
    }
}

try {
    Write-Host "Checking for Python 3.10 or newer..."
    if (!(Set-PythonCommand)) {
        $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
        if (!$winget) {
            Start-Process "https://www.python.org/downloads/windows/"
            throw "Python 3.10 or newer is required. Install it from the page that was opened, then run RoomTrace.cmd again."
        }

        Write-Host "Python was not found. Installing Python 3.12 for the current user..."
        & $winget.Source install --id Python.Python.3.12 --exact --scope user --accept-package-agreements --accept-source-agreements --silent
        if ($LASTEXITCODE -ne 0) {
            throw "winget could not install Python. Install Python 3.10 or newer manually and run RoomTrace.cmd again."
        }
        if (!(Set-PythonCommand)) {
            throw "Python was installed, but this terminal could not find it yet. Close this window, open RoomTrace.cmd again, and retry."
        }
    }

    if (!(Test-Path $venvPython)) {
        Write-Host "Creating the RoomTrace environment..."
        Invoke-SystemPython @("-m", "venv", $venvRoot)
    }
    if (!(Test-Path $venvPython)) {
        throw "The Python virtual environment could not be created: $venvRoot"
    }

    $installedVersion = if (Test-Path $markerPath) { (Get-Content $markerPath -Raw).Trim() } else { "" }
    if ($installedVersion -ne $setupVersion) {
        Write-Host "Installing RoomTrace and its dependencies..."
        & $venvPython -m pip install --disable-pip-version-check -e $processorRoot
        if ($LASTEXITCODE -ne 0) {
            throw "Package installation failed. Check the network connection and run RoomTrace.cmd again."
        }
        Set-Content -Path $markerPath -Value $setupVersion -Encoding ASCII
    }

    try {
        $desktop = [Environment]::GetFolderPath("Desktop")
        if ($desktop) {
            $shortcutPath = Join-Path $desktop "RoomTrace.lnk"
            $shell = New-Object -ComObject WScript.Shell
            $shortcut = $shell.CreateShortcut($shortcutPath)
            $shortcut.TargetPath = Join-Path $projectRoot "RoomTrace.cmd"
            $shortcut.WorkingDirectory = $projectRoot
            $shortcut.Description = "RoomTrace capture processor"
            $shortcut.Save()
            [void][Runtime.InteropServices.Marshal]::ReleaseComObject($shell)
            Write-Host "Desktop shortcut created: $shortcutPath"
        }
    } catch {
        Write-Host "Desktop shortcut could not be created; you can still run RoomTrace.cmd from this folder."
    }

    Write-Host "RoomTrace setup is complete."
    exit 0
} catch {
    Write-Host "ERROR: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
