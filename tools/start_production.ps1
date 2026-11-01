param(
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

$localEnv = Join-Path $projectRoot ".env"
if (Test-Path -LiteralPath $localEnv) {
    Get-Content -LiteralPath $localEnv -Encoding UTF8 | ForEach-Object {
        $line = $_.Trim()
        if ($line -and -not $line.StartsWith("#") -and $line -match '^([A-Za-z_][A-Za-z0-9_]*)=(.*)$') {
            $name = $Matches[1]
            $value = $Matches[2].Trim()
            if (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'"))) {
                $value = $value.Substring(1, $value.Length - 2)
            }
            if (-not [Environment]::GetEnvironmentVariable($name, "Process")) {
                [Environment]::SetEnvironmentVariable($name, $value, "Process")
            }
        }
    }
}

if (-not (Test-Path -LiteralPath (Join-Path $projectRoot "app.py"))) {
    throw "Run this script from a Personal OS repository checkout. app.py was not found."
}

if ($Python -eq "python" -and (Test-Path -LiteralPath (Join-Path $projectRoot ".venv\Scripts\python.exe"))) {
    $Python = Join-Path $projectRoot ".venv\Scripts\python.exe"
}

$env:PERSONAL_OS_ENV = "production"
$env:PERSONAL_OS_PORT = "8788"
Remove-Item Env:PERSONAL_OS_DB_PATH -ErrorAction SilentlyContinue
Remove-Item Env:PERSONAL_OS_BACKUP_DIR -ErrorAction SilentlyContinue
Remove-Item Env:PERSONAL_OS_ATTACHMENT_DIR -ErrorAction SilentlyContinue

Write-Host "Personal OS production: http://localhost:8788"
$lanAddress = [System.Net.Dns]::GetHostAddresses([System.Net.Dns]::GetHostName()) |
    Where-Object { $_.AddressFamily -eq [System.Net.Sockets.AddressFamily]::InterNetwork -and $_.IPAddressToString -notlike '127.*' -and $_.IPAddressToString -notlike '169.254.*' } |
    Select-Object -First 1 -ExpandProperty IPAddressToString
if ([string]::IsNullOrWhiteSpace($lanAddress)) { $lanAddress = '127.0.0.1' }
Write-Host "iPhone (same Wi-Fi): http://${lanAddress}:8788"
Push-Location $projectRoot
try {
    & $Python "app.py"
}
finally {
    Pop-Location
}
