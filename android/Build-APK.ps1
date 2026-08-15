$ErrorActionPreference = "Stop"

$androidRoot = $PSScriptRoot
$projectRoot = Split-Path -Parent $androidRoot
$gradleVersion = "8.7"

function Find-AndroidSdk {
    $candidates = @()
    $sdkRoot = [Environment]::GetEnvironmentVariable("ANDROID_SDK_ROOT")
    $androidHome = [Environment]::GetEnvironmentVariable("ANDROID_HOME")
    if ($sdkRoot) { $candidates += $sdkRoot }
    if ($androidHome) { $candidates += $androidHome }
    if ($env:LOCALAPPDATA) { $candidates += Join-Path $env:LOCALAPPDATA "Android\Sdk" }
    foreach ($candidate in ($candidates | Select-Object -Unique)) {
        if ($candidate -and (Test-Path $candidate)) {
            return (Resolve-Path $candidate).Path
        }
    }
    return $null
}

function Find-JavaHome {
    $javaCandidates = @()
    $javaHome = [Environment]::GetEnvironmentVariable("JAVA_HOME")
    if ($javaHome) { $javaCandidates += Join-Path $javaHome "bin\java.exe" }
    if ($env:ProgramFiles) {
        $javaCandidates += Join-Path $env:ProgramFiles "Android\Android Studio\jbr\bin\java.exe"
        $javaCandidates += Join-Path $env:ProgramFiles "Android\Android Studio\jre\bin\java.exe"
    }
    $javaCommand = Get-Command java.exe -ErrorAction SilentlyContinue
    if ($javaCommand) { $javaCandidates += $javaCommand.Source }
    foreach ($candidate in ($javaCandidates | Select-Object -Unique)) {
        if ($candidate -and (Test-Path $candidate)) {
            return Split-Path -Parent (Split-Path -Parent (Resolve-Path $candidate).Path)
        }
    }
    return $null
}

function Find-Gradle {
    $command = Get-Command gradle.bat -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }

    $toolsRoot = Join-Path $projectRoot ".tools"
    $gradleRoot = Join-Path $toolsRoot "gradle-$gradleVersion"
    $gradleExe = Join-Path $gradleRoot "bin\gradle.bat"
    if (!(Test-Path $gradleExe)) {
        New-Item -ItemType Directory -Path $toolsRoot -Force | Out-Null
        $archive = Join-Path $toolsRoot "gradle-$gradleVersion-bin.zip"
        if (!(Test-Path $archive)) {
            Write-Host "Downloading Gradle $gradleVersion..."
            Invoke-WebRequest -Uri "https://services.gradle.org/distributions/gradle-$gradleVersion-bin.zip" -OutFile $archive
        }
        if (!(Test-Path $gradleRoot)) {
            Expand-Archive -Path $archive -DestinationPath $toolsRoot -Force
        }
    }
    if (!(Test-Path $gradleExe)) {
        throw "Gradle $gradleVersion could not be prepared. Install Gradle or run Android Studio once, then retry."
    }
    return $gradleExe
}

try {
    $sdk = Find-AndroidSdk
    if (!$sdk) {
        throw "Android SDK was not found. Install Android Studio, open this android folder once, and retry."
    }

    $java = Find-JavaHome
    if (!$java) {
        throw "JDK 17 was not found. Install Android Studio with its bundled JDK, then retry."
    }

    $env:ANDROID_SDK_ROOT = $sdk
    $env:ANDROID_HOME = $sdk
    $env:JAVA_HOME = $java
    Write-Host "Using Android SDK: $sdk"
    Write-Host "Using JDK: $java"

    $localProperties = Join-Path $androidRoot "local.properties"
    $escapedSdk = $sdk -replace '\\', '\\\\'
    $otherProperties = @()
    if (Test-Path $localProperties) {
        $otherProperties = Get-Content $localProperties | Where-Object { $_ -notmatch '^sdk\.dir=' }
    }
    @("sdk.dir=$escapedSdk"; $otherProperties) | Set-Content -Path $localProperties -Encoding ASCII

    $gradle = Find-Gradle
    Write-Host "Building the RoomTrace capture APK..."
    & $gradle -p $androidRoot assembleDebug --no-daemon --stacktrace
    if ($LASTEXITCODE -ne 0) {
        throw "Gradle failed with exit code $LASTEXITCODE"
    }

    $apk = Join-Path $androidRoot "app\build\outputs\apk\debug\app-debug.apk"
    if (!(Test-Path $apk)) {
        throw "Gradle reported success, but the APK was not found: $apk"
    }
    $destination = Join-Path $projectRoot "RoomTrace-Capture.apk"
    Copy-Item -Path $apk -Destination $destination -Force
    Write-Host "APK ready: $destination" -ForegroundColor Green
    exit 0
} catch {
    Write-Host "ERROR: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
