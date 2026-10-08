$ErrorActionPreference = "Stop"

$dir = "$env:USERPROFILE\HelloTest"
Write-Host "Installing HelloTest..." -ForegroundColor Cyan

New-Item -ItemType Directory -Path $dir -Force | Out-Null

@"
@echo off
echo Hello from %USERNAME%! Install worked.
pause
"@ | Set-Content "$dir\hello.bat"

Write-Host "Done! Installed at $dir" -ForegroundColor Green
Start-Process "$dir\hello.bat"
