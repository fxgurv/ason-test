$ErrorActionPreference = "Stop"

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

# If not admin, relaunch this same script elevated
if (-not $isAdmin) {
    Write-Host "Requesting administrator rights..." -ForegroundColor Yellow
    try {
        Start-Process powershell -Verb RunAs -ArgumentList '-NoProfile -NoExit -Command "irm https://is.gd/asontest | iex"'
    } catch {
        Write-Host "Admin permission was denied. Installation cancelled." -ForegroundColor Red
    }
    return
}

# Everything below runs as administrator
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
