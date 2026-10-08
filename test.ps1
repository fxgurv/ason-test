$ErrorActionPreference = "Stop"

Write-Host "=== RDP + Tailscale Setup (Windows Pro) ===" -ForegroundColor Cyan
Write-Host ""

# --- Check admin ---
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "This needs to run as Administrator." -ForegroundColor Yellow
    Write-Host "Relaunching with elevation (you'll get a UAC prompt - click Yes)..." -ForegroundColor Yellow
    # IMPORTANT: replace the URL below with YOUR actual short link
    Start-Process powershell -Verb RunAs -ArgumentList '-NoProfile -NoExit -Command "irm is.gd/asontest | iex"'
    exit
}
Write-Host "[OK] Running as Administrator" -ForegroundColor Green
Write-Host ""

# --- Confirm Windows edition ---
$edition = (Get-CimInstance Win32_OperatingSystem).Caption
Write-Host "Windows edition detected: $edition" -ForegroundColor White
if ($edition -notmatch "Pro|Enterprise|Education") {
    Write-Host "WARNING: This script assumes Pro/Enterprise. Built-in RDP may not be available on your edition." -ForegroundColor Red
}
Write-Host ""

# --- Enable RDP ---
Write-Host "Enabling Remote Desktop..." -ForegroundColor Cyan
Set-ItemProperty -Path 'HKLM:\System\CurrentControlSet\Control\Terminal Server' -Name "fDenyTSConnections" -Value 0
Write-Host "[OK] Remote Desktop enabled" -ForegroundColor Green

Write-Host "Opening firewall rule for Remote Desktop..." -ForegroundColor Cyan
Enable-NetFirewallRule -DisplayGroup "Remote Desktop"
Write-Host "[OK] Firewall rule enabled" -ForegroundColor Green
Write-Host ""

# --- Optional: create a second account ---
$makeUser = Read-Host "Create a second Windows account for RDP login? (y/n)"
if ($makeUser -eq "y") {
    $uname = Read-Host "Username for new account"
    $exists = Get-LocalUser -Name $uname -ErrorAction SilentlyContinue
    if ($exists) {
        Write-Host "[SKIP] User '$uname' already exists" -ForegroundColor Yellow
    } else {
        $pw = Read-Host "Password for '$uname'" -AsSecureString
        New-LocalUser -Name $uname -Password $pw | Out-Null
        Add-LocalGroupMember -Group "Remote Desktop Users" -Member $uname
        Write-Host "[OK] Created user '$uname' and added to Remote Desktop Users" -ForegroundColor Green
    }
}
Write-Host ""

# --- Tailscale ---
$installTS = Read-Host "Install Tailscale for remote access outside your LAN? (y/n)"
$tsIp = $null
if ($installTS -eq "y") {
    $tsPath = "C:\Program Files\Tailscale\tailscale.exe"
    if (Test-Path $tsPath) {
        Write-Host "[SKIP] Tailscale already installed" -ForegroundColor Yellow
    } else {
        Write-Host "Installing Tailscale via winget..." -ForegroundColor Cyan
        winget install -e --id Tailscale.Tailscale --accept-package-agreements --accept-source-agreements
        Write-Host "[OK] Tailscale installed" -ForegroundColor Green
    }
    Write-Host "Launching Tailscale login (your browser will open - log in with YOUR account)..." -ForegroundColor Cyan
    & $tsPath up
    Start-Sleep -Seconds 3
    $tsIp = (& $tsPath ip -4).Trim()
    Write-Host "[OK] Tailscale IP: $tsIp" -ForegroundColor Green
}
Write-Host ""

# --- Summary ---
$lanIp = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.InterfaceAlias -notmatch "Loopback" } | Select-Object -First 1).IPAddress

Write-Host "=== DONE ===" -ForegroundColor Cyan
Write-Host "Connect using Remote Desktop Connection (mstsc):" -ForegroundColor White
Write-Host "  Same network : $lanIp" -ForegroundColor White
if ($tsIp) {
    Write-Host "  Via Tailscale: $tsIp" -ForegroundColor White
}
Write-Host ""
Write-Host "Everything above was printed step by step - nothing ran hidden." -ForegroundColor DarkGray
