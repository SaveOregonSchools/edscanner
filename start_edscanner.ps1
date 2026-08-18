$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonPath = Join-Path $projectRoot ".venv\Scripts\python.exe"
$serverLauncherPath = Join-Path $projectRoot "run_edscanner_server.ps1"
$appUrl = "http://127.0.0.1:8765"

function Test-EdScannerRunning {
    try {
        $response = Invoke-WebRequest -Uri $appUrl -UseBasicParsing -TimeoutSec 1
        return $response.StatusCode -ge 200 -and $response.StatusCode -lt 500
    }
    catch {
        return $false
    }
}

if (-not (Test-Path -LiteralPath $pythonPath)) {
    Add-Type -AssemblyName PresentationFramework
    [System.Windows.MessageBox]::Show(
        "EdScanner's Python environment was not found at:`n$pythonPath",
        "EdScanner could not start",
        "OK",
        "Error"
    ) | Out-Null
    exit 1
}

if (-not (Test-Path -LiteralPath $serverLauncherPath)) {
    Add-Type -AssemblyName PresentationFramework
    [System.Windows.MessageBox]::Show(
        "EdScanner's visible server launcher was not found at:`n$serverLauncherPath",
        "EdScanner could not start",
        "OK",
        "Error"
    ) | Out-Null
    exit 1
}

if (-not (Test-EdScannerRunning)) {
    Start-Process `
        -FilePath "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" `
        -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$serverLauncherPath`"") `
        -WorkingDirectory $projectRoot `
        -WindowStyle Normal

    $started = $false
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        Start-Sleep -Milliseconds 500
        if (Test-EdScannerRunning) {
            $started = $true
            break
        }
    }

    if (-not $started) {
        Add-Type -AssemblyName PresentationFramework
        [System.Windows.MessageBox]::Show(
            "EdScanner did not become available at $appUrl.`nCheck logs\edscanner.log for details.",
            "EdScanner could not start",
            "OK",
            "Error"
        ) | Out-Null
        exit 1
    }
}

Start-Process $appUrl
