# install.ps1 - RDPSetup.bat isay chalati hai, aap isay seedha na chalayein.
$ErrorActionPreference = 'SilentlyContinue'
$ScriptDir  = $PSScriptRoot
$InstallDir = 'C:\ProgramData\RDPMulti'
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null

# ---- Read logging setting from config.ini (default = yes) ----
$Logging = $true
$cfgPath = Join-Path $ScriptDir 'config.ini'
if (Test-Path $cfgPath) {
    $raw = Get-Content $cfgPath -Raw -ErrorAction SilentlyContinue
    if ($raw -match '(?im)^\s*logging\s*=\s*(no|false|0)\s*$') {
        $Logging = $false
    }
}

# If logging=no, hide this console window
if (-not $Logging) {
    Add-Type -Name Win -Namespace Native -MemberDefinition @'
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
    [DllImport("kernel32.dll")] public static extern IntPtr GetConsoleWindow();
'@
    $hwnd = [Native.Win]::GetConsoleWindow()
    if ($hwnd -ne [IntPtr]::Zero) { [Native.Win]::ShowWindow($hwnd, 0) | Out-Null }  # SW_HIDE
}

Start-Transcript -Path "$InstallDir\bootstrap.log" -Append | Out-Null

# bgpc.py (aur config.ini agar ho) USB se install folder mein copy
if (-not (Test-Path "$ScriptDir\bgpc.py")) {
    Write-Output "ERROR: bgpc.py nahi mili. Isay USB mein install.ps1 ke saath rakhein."
    Stop-Transcript | Out-Null
    if ($Logging) { Read-Host "Enter dabayein..." }
    exit 1
}
Copy-Item "$ScriptDir\bgpc.py" "$InstallDir\bgpc.py" -Force
if (Test-Path "$ScriptDir\config.ini") {
    Copy-Item "$ScriptDir\config.ini" "$InstallDir\config.ini" -Force
}

# Pehle se koi usable Python ho to wahi use karein (Microsoft Store wala stub nahi)
function Find-Python {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source -notmatch 'WindowsApps') { return $cmd.Source }
    $local = "$InstallDir\python\python.exe"
    if (Test-Path $local) { return $local }
    return $null
}

$pyExe = Find-Python
if (-not $pyExe) {
    Write-Output "Python nahi mila, portable Python (sirf is folder mein, system-wide install nahi) download kar raha hoon..."
    $pyDir = "$InstallDir\python"
    New-Item -ItemType Directory -Force -Path $pyDir | Out-Null
    $zip = "$env:TEMP\python-embed.zip"
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    try {
        Invoke-WebRequest -UseBasicParsing -Uri "https://www.python.org/ftp/python/3.12.4/python-3.12.4-embed-amd64.zip" -OutFile $zip
        Expand-Archive -Path $zip -DestinationPath $pyDir -Force
        if (Test-Path "$pyDir\python.exe") { $pyExe = "$pyDir\python.exe" }
    } catch {
        Write-Output "Portable Python download fail: $_"
    }
}

if (-not $pyExe -or -not (Test-Path $pyExe)) {
    Write-Output "ERROR: Python install nahi ho saka. Internet connection check karein aur dobara koshish karein."
    Stop-Transcript | Out-Null
    if ($Logging) { Read-Host "Enter dabayein..." }
    exit 1
}

Write-Output "Python: $pyExe"
Write-Output "Logging: $(if ($Logging) {'YES (terminal visible)'} else {'NO (background)'})"
Write-Output "Ab bgpc.py --silent chala raha hoon..."
& $pyExe "$InstallDir\bgpc.py" --silent
$exitCode = $LASTEXITCODE
Write-Output "Khatam. (exit $exitCode)"
Stop-Transcript | Out-Null
exit $exitCode
