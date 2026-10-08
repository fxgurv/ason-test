$ErrorActionPreference = "Stop"

# Check if running as Administrator
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

if (-not $isAdmin) {
    Write-Host "Requesting administrator rights..." -ForegroundColor Yellow
    try {
        # Yahan apni GitHub raw URL daali hai
        Start-Process powershell -Verb RunAs -ArgumentList '-NoProfile -NoExit -Command "irm https://raw.githubusercontent.com/YOURNAME/ason-test/main/test.ps1 | iex"'
    }
    catch {
        Write-Host "Admin permission was denied. Installation cancelled." -ForegroundColor Red
    }
    return
}

# ====================== ADMIN SECTION ======================
Write-Host "Running as ADMIN: $isAdmin" -ForegroundColor Green

$dir = "$env:USERPROFILE\HelloTest"
New-Item -ItemType Directory -Path $dir -Force | Out-Null

@"
@echo off
echo Hello from %USERNAME%! Install worked.
echo Admin test passed.
pause
"@ | Set-Content "$dir\hello.bat"

Write-Host "Done! Installed at $dir" -ForegroundColor Green
Start-Process "$dir\hello.bat"
