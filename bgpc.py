"""
bgpc.py  (v4)  ->  RDPSetup.exe
Windows PC par dost ko alag parallel RDP session (full admin) deti hai.

Do tareeqe:
  1) EXE (USB se):  RDPSetup.exe chalayein (sirf UAC "Yes" dabana hai).
     Ye SILENT chalta hai, settings config.ini se leta hai (exe ke saath rakhein),
     aur natija RDP_RESULT_<PCNAME>.txt mein USB par likh deta hai.
  2) Script:        py bgpc.py      (interactive, sawal poochta hai)

Khud karti hai:
  - RDP ON, multi-session, "ek user = ek session"
  - PC kabhi sleep/hibernate na ho, network adapter band na ho, update restart na kare
  - RDP session kabhi khud band na ho + keep-alive
  - 'friend' user = Administrator + Remote Desktop Users
  - RDP Wrapper + sahi rdpwrap.ini (multiple sources + offset finder fallback)
  - Tailscale install (winget -> MSI -> EXE) + login + unattended
  - Har boot par AutoFix task (Windows update ke baad ini khud theek)
"""
import configparser
import ctypes
import getpass
import io
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import zipfile

try:
    import winreg
except ImportError:
    sys.exit("Ye script sirf Windows ke liye hai.")

FROZEN = getattr(sys, "frozen", False)
AUTO = "--auto" in sys.argv                                   # boot par chalne wala silent mode
SILENT = AUTO or FROZEN or "--silent" in sys.argv            # koi sawal nahi poochna
BASE_DIR = os.path.dirname(os.path.abspath(sys.executable if FROZEN else __file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.ini")
NOWIN = 0x08000000                                            # CREATE_NO_WINDOW

CFG = {"friend_user": "friend", "friend_password": "", "friend_admin": True,
       "reset_password": False, "auto_restart": False, "show_popup": True,
       "tailscale_authkey": ""}
FRIEND = "friend"
STATE = {"login_url": None}

WRAP_ZIP = "https://github.com/stascorp/rdpwrap/releases/download/v1.6.2/RDPWrap-v1.6.2.zip"
INI_SOURCES = [
    "https://raw.githubusercontent.com/sebaxakerhtc/rdpwrap.ini/master/rdpwrap.ini",
    "https://raw.githubusercontent.com/affinityv/INI-RDPWRAP/master/rdpwrap.ini",
    "https://raw.githubusercontent.com/stascorp/rdpwrap/master/res/rdpwrap.ini",
]
OFFSET_FINDER_API = "https://api.github.com/repos/llccd/RDPWrapOffsetFinder/releases/latest"
TAILSCALE_URLS = [
    "https://pkgs.tailscale.com/stable/tailscale-setup-latest-amd64.msi",
    "https://pkgs.tailscale.com/stable/tailscale-setup-latest.exe",
]
WRAP_DIR = r"C:\Program Files\RDP Wrapper"
INI_PATH = os.path.join(WRAP_DIR, "rdpwrap.ini")
INSTALL_DIR = r"C:\ProgramData\RDPMulti"
TASK_NAME = "RDPMulti-AutoFix"
HKLM = winreg.HKEY_LOCAL_MACHINE
TS_KEY = r"SYSTEM\CurrentControlSet\Control\Terminal Server"
TS_POLICY = r"SOFTWARE\Policies\Microsoft\Windows NT\Terminal Services"

CONFIG_TEMPLATE = """[setup]
; Dost ke Windows user ka naam
friend_user = friend

; Dost ka password (kam az kam 6 characters). ZAROORI hai.
friend_password =

; yes = dost Administrator hoga, no = normal user
friend_admin = yes

; yes = agar user pehle se hai to uska password bhi isi password par reset kar do
reset_password = no

; yes = kaam ke baad PC 60 second mein khud restart ho jaye
auto_restart = no

; yes = kaam ke baad chhota message box dikhao
show_popup = yes

; Optional: Tailscale auth key (login.tailscale.com -> Settings -> Keys).
; Khali chhorein to browser mein login ka link khud khulega.
tailscale_authkey =
"""


# ----------------------------------------------------------------- helpers
def run(cmd, **kw):
    """Bina console window ke command chalata hai (exe mein bhi theek)."""
    kw.setdefault("creationflags", NOWIN)
    kw.setdefault("stdin", subprocess.DEVNULL)
    return subprocess.run(cmd, capture_output=True, text=True, errors="replace", **kw)


def ps(expr):
    return run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", expr]).stdout.strip()


def yes(v):
    return str(v).strip().lower() in ("yes", "y", "true", "1")


def popup(text, error=False):
    if AUTO or not SILENT:
        return
    if not error and not CFG["show_popup"]:
        return
    try:
        ctypes.windll.user32.MessageBoxW(0, text, "RDP Setup", 0x10 if error else 0x40)
    except Exception:
        pass


def fatal(msg):
    print("ERROR:", msg)
    if SILENT:
        popup(msg, error=True)
        sys.exit(1)
    sys.exit(msg)


def setup_logging():
    os.makedirs(INSTALL_DIR, exist_ok=True)
    f = open(os.path.join(INSTALL_DIR, "autofix.log" if AUTO else "setup.log"),
             "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = f
    print("\n=== ", time.strftime("%Y-%m-%d %H:%M:%S"), "AUTO" if AUTO else "SETUP", "===")


def load_config():
    global FRIEND
    if not os.path.exists(CONFIG_PATH):
        if SILENT:
            try:
                with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                    f.write(CONFIG_TEMPLATE)
            except OSError:
                pass
        return   # config optional hai: default settings se chalega
    cp = configparser.ConfigParser(inline_comment_prefixes=None)
    cp.read(CONFIG_PATH, encoding="utf-8-sig")
    s = cp["setup"] if cp.has_section("setup") else {}
    CFG["friend_user"] = (s.get("friend_user", "friend") or "friend").strip()
    CFG["friend_password"] = (s.get("friend_password", "") or "").strip()
    CFG["friend_admin"] = yes(s.get("friend_admin", "yes"))
    CFG["reset_password"] = yes(s.get("reset_password", "no"))
    CFG["auto_restart"] = yes(s.get("auto_restart", "no"))
    CFG["show_popup"] = yes(s.get("show_popup", "yes"))
    CFG["tailscale_authkey"] = (s.get("tailscale_authkey", "") or "").strip()
    FRIEND = CFG["friend_user"]


def http_get(url, timeout=120):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except Exception as e:
        if "CERTIFICATE_VERIFY_FAILED" not in str(e):
            raise
        # Python ke cert store mein masla: Windows ke apne cert store (curl/PowerShell) se download
        path = os.path.join(tempfile.mkdtemp(), "dl.bin")
        run(["curl.exe", "-L", "-s", "-f", "-A", "Mozilla/5.0", "-o", path, url])
        if not (os.path.exists(path) and os.path.getsize(path) > 0):
            ps("[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; "
               f"Invoke-WebRequest -UseBasicParsing -UserAgent 'Mozilla/5.0' -Uri '{url}' -OutFile '{path}'")
        if os.path.exists(path) and os.path.getsize(path) > 0:
            with open(path, "rb") as f:
                return f.read()
        raise


def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


def ensure_admin():
    if is_admin():
        return
    args = sys.argv[1:] if FROZEN else [os.path.abspath(sys.argv[0])] + sys.argv[1:]
    params = " ".join(f'"{a}"' for a in args)
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, None, 0 if SILENT else 1)
    if rc <= 32:
        fatal("Admin permission nahi mili (UAC mein Yes dabana zaroori hai).")
    sys.exit(0)


def set_reg(path, name, value):
    with winreg.CreateKeyEx(HKLM, path, 0, winreg.KEY_WRITE | winreg.KEY_WOW64_64KEY) as k:
        winreg.SetValueEx(k, name, 0, winreg.REG_DWORD, value)


def termsrv_version():
    # FileVersion ka text kabhi 10.0.19041.1 dikhata hai; asli binary version parts se lein
    out = ps(r"$v=(Get-Item $env:windir\System32\termsrv.dll).VersionInfo; "
             r"'{0}.{1}.{2}.{3}' -f $v.FileMajorPart,$v.FileMinorPart,$v.FileBuildPart,$v.FilePrivatePart")
    m = re.match(r"(\d+\.\d+\.\d+\.\d+)", out)
    return m.group(1) if m else None


def wrapper_installed():
    try:
        with winreg.OpenKey(HKLM, r"SYSTEM\CurrentControlSet\Services\TermService\Parameters") as k:
            return "rdpwrap" in str(winreg.QueryValueEx(k, "ServiceDll")[0]).lower()
    except OSError:
        return False


def restart_termservice():
    run(["net", "stop", "termservice", "/y"])
    time.sleep(2)
    run(["net", "start", "termservice"])
    run(["net", "start", "umrdpservice"])


# ------------------------------------------------------------------- RDP
def enable_rdp():
    set_reg(TS_KEY, "fDenyTSConnections", 0)
    set_reg(TS_KEY, "fSingleSessionPerUser", 1)   # ek user = ek session; reconnect par wahi wapis khulta hai
    set_reg(TS_KEY + r"\Licensing Core", "EnableConcurrentSessions", 1)
    set_reg(r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon", "AllowMultipleTSSessions", 1)

    set_reg(TS_POLICY, "fSingleSessionPerUser", 1)
    set_reg(TS_POLICY, "MaxInstanceCount", 999999)
    set_reg(TS_POLICY, "MaxIdleTime", 0)
    set_reg(TS_POLICY, "MaxDisconnectionTime", 0)
    set_reg(TS_POLICY, "MaxConnectionTime", 0)
    set_reg(TS_POLICY, "KeepAliveEnable", 1)
    set_reg(TS_POLICY, "KeepAliveInterval", 1)
    set_reg(TS_POLICY, "fResetBroken", 0)

    run(["sc", "config", "TermService", "start=", "auto"])
    run(["sc", "failure", "TermService", "reset=", "86400",
         "actions=", "restart/5000/restart/5000/restart/5000"])

    rule = "RDP-Friend-3389"                       # firewall: sirf LAN + Tailscale range
    run(["netsh", "advfirewall", "firewall", "delete", "rule", f"name={rule}"])
    r = run(["netsh", "advfirewall", "firewall", "add", "rule", f"name={rule}", "dir=in",
             "action=allow", "protocol=TCP", "localport=3389",
             "remoteip=LocalSubnet,100.64.0.0/10", "profile=any"])
    print("RDP ON + multi-session settings ho gayi." if r.returncode == 0
          else "RDP ON, magar firewall rule add nahi hui: " + r.stdout.strip())


def no_sleep():
    for c in ("standby-timeout-ac", "standby-timeout-dc", "hibernate-timeout-ac",
              "hibernate-timeout-dc", "disk-timeout-ac", "disk-timeout-dc"):
        run(["powercfg", "/change", c, "0"])
    run(["powercfg", "/hibernate", "off"])
    for fn in ("/setacvalueindex", "/setdcvalueindex"):       # laptop ka lid band karne par kuch na ho
        run(["powercfg", fn, "SCHEME_CURRENT", "SUB_BUTTONS", "LIDACTION", "0"])
    run(["powercfg", "/setactive", "SCHEME_CURRENT"])
    set_reg(r"SYSTEM\CurrentControlSet\Control\Session Manager\Power", "HiberbootEnabled", 0)
    ps("Get-NetAdapter -Physical -ErrorAction SilentlyContinue | ForEach-Object { "
       "Set-NetAdapterPowerManagement -Name $_.Name -AllowComputerToTurnOffDevice Disabled "
       "-ErrorAction SilentlyContinue }")
    set_reg(r"SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU", "NoAutoRebootWithLoggedOnUsers", 1)
    print("Sleep/hibernate band, power settings theek.")


def create_friend():
    def ask_password():
        for _ in range(3):
            pw = getpass.getpass(f"'{FRIEND}' ka password (kam az kam 6 characters): ")
            if len(pw) < 6:
                print("Password chhota hai.")
                continue
            if getpass.getpass("Password dobara likhein: ") != pw:
                print("Dono password alag hain.")
                continue
            return pw
        fatal("Password sahi set nahi hua.")

    pw_cfg = CFG["friend_password"]
    if pw_cfg and len(pw_cfg) < 6:
        fatal("config.ini mein friend_password kam az kam 6 characters ka hona chahiye.")

    exists = run(["net", "user", FRIEND]).returncode == 0
    if exists:
        print(f"User '{FRIEND}' pehle se hai.")
        if SILENT:
            reset = CFG["reset_password"] and bool(pw_cfg)
            pw = pw_cfg
        else:
            reset = input("Password reset karna hai? (y/N): ").strip().lower() == "y"
            pw = pw_cfg or (ask_password() if reset else "")
        if reset:
            if run(["net", "user", FRIEND, pw]).returncode != 0:
                fatal("Password reset nahi hua.")
            print("Password badal diya.")
    else:
        pw = pw_cfg or (None if SILENT else ask_password())
        if not pw:
            # Silent mode mein password nahi diya: khud mazboot password bana do (RDP_RESULT file mein likha jayega)
            pw = secrets.token_urlsafe(9) + "aA1!"
            STATE["generated_pw"] = pw
        r = run(["net", "user", FRIEND, pw, "/add"])
        if r.returncode != 0:
            fatal("User nahi bana (password policy ya naam ka masla): " + (r.stdout + r.stderr).strip())
        print(f"User '{FRIEND}' ban gaya.")

    # SID se group (Windows ki kisi bhi language mein kaam karta hai)
    ps(f"Add-LocalGroupMember -SID S-1-5-32-555 -Member '{FRIEND}' -ErrorAction SilentlyContinue")  # Remote Desktop Users
    if CFG["friend_admin"]:
        ps(f"Add-LocalGroupMember -SID S-1-5-32-544 -Member '{FRIEND}' -ErrorAction SilentlyContinue")  # Administrators
    else:
        ps(f"Remove-LocalGroupMember -SID S-1-5-32-544 -Member '{FRIEND}' -ErrorAction SilentlyContinue")
    ps(f"Enable-LocalUser -Name '{FRIEND}' -ErrorAction SilentlyContinue")
    ps(f"Set-LocalUser -Name '{FRIEND}' -PasswordNeverExpires $true -ErrorAction SilentlyContinue")


def friend_is_admin():
    return FRIEND.lower() in ps("(Get-LocalGroupMember -SID S-1-5-32-544).Name -join ','").lower()


# ------------------------------------------------------------ RDP Wrapper
def find_offsets(ver):
    """Fallback: apne termsrv.dll se offsets nikaal kar ini section banata hai."""
    print("Ini mein ye version nahi mila. RDPWrapOffsetFinder se offsets nikaal raha hoon...")
    try:
        rel = json.loads(http_get(OFFSET_FINDER_API))
        asset = next(a for a in rel["assets"] if a["name"].lower().endswith(".zip"))
        d = tempfile.mkdtemp()
        ps(f"Add-MpPreference -ExclusionPath '{d}'")
        zipfile.ZipFile(io.BytesIO(http_get(asset["browser_download_url"], 180))).extractall(d)
        exe = None
        for root, _, files in os.walk(d):
            for f in files:
                if f.lower() == "rdpwrapoffsetfinder.exe":
                    exe = os.path.join(root, f)
        if not exe:
            return None
        r = run([exe], cwd=os.path.dirname(exe), timeout=300)
        marker = f"[{ver}]"
        if marker in r.stdout:
            return r.stdout[r.stdout.index(marker):]
    except Exception as e:
        print("Offset finder nahi chala:", e)
    return None


def update_ini(ver):
    current = ""
    if os.path.exists(INI_PATH):
        with open(INI_PATH, "r", encoding="utf-8", errors="ignore") as f:
            current = f.read()
    if ver and f"[{ver}]" in current:
        print(f"rdpwrap.ini mein version {ver} pehle se supported hai.")
        return True

    base, new_text = None, None
    for url in INI_SOURCES:
        try:
            text = http_get(url).decode("utf-8", "ignore")
        except Exception as e:
            print("  ini download fail:", url.split("/")[3], "-", e)
            continue
        if base is None:
            base = text
        if not ver or f"[{ver}]" in text:
            new_text = text
            print("  Sahi ini mili:", url.split("/")[3])
            break

    if new_text is None and ver:
        section = find_offsets(ver)
        if section:
            new_text = (base or current) + "\r\n\r\n" + section + "\r\n"

    if new_text is None:
        print(f"WARNING: version {ver} ke liye koi ini nahi mili. Multi-session shayad na chale.")
        if base:
            new_text = base
        else:
            return False

    run(["net", "stop", "termservice", "/y"])
    time.sleep(2)
    with open(INI_PATH, "w", encoding="utf-8", newline="") as f:
        f.write(new_text)
    run(["net", "start", "termservice"])
    run(["net", "start", "umrdpservice"])
    return bool(ver and f"[{ver}]" in new_text)


def install_wrapper(ver):
    tmp = tempfile.mkdtemp()
    ps(f"Add-MpPreference -ExclusionPath '{WRAP_DIR}','{tmp}'")   # Defender pehle se exclusion
    if wrapper_installed():
        print("RDP Wrapper pehle se installed hai, sirf ini check/update kar raha hoon.")
    else:
        print("RDP Wrapper download ho raha hai...")
        zipfile.ZipFile(io.BytesIO(http_get(WRAP_ZIP))).extractall(tmp)
        exe = None
        for root, _, files in os.walk(tmp):
            if "RDPWInst.exe" in files:
                exe = os.path.join(root, "RDPWInst.exe")
        if not exe:
            fatal("RDPWInst.exe nahi mila (Defender ne delete kar diya ho sakta hai).")
        r = run([exe, "-i"])
        print(r.stdout)
        if r.returncode != 0 and not wrapper_installed():
            fatal("RDP Wrapper install nahi hua. Log: " + os.path.join(INSTALL_DIR, "setup.log"))
    return update_ini(ver)


# --------------------------------------------------------------- Tailscale
def find_tailscale():
    cands = [shutil.which("tailscale"),
             r"C:\Program Files\Tailscale\tailscale.exe",
             r"C:\Program Files (x86)\Tailscale\tailscale.exe"]
    return next((c for c in cands if c and os.path.exists(c)), None)


def tailscale_ip(ts):
    out = run([ts, "ip", "-4"]).stdout.strip()
    return out.splitlines()[0] if out else None


def tailscale_login(ts):
    key = CFG["tailscale_authkey"]
    if key:
        try:
            run([ts, "up", "--auth-key", key], timeout=180)
        except subprocess.TimeoutExpired:
            print("Tailscale auth key login ka wait khatam.")
        return
    p = subprocess.Popen([ts, "up"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         stdin=subprocess.DEVNULL, text=True, errors="replace", creationflags=NOWIN)

    def reader():
        for line in p.stdout:
            m = re.search(r"https://login\.tailscale\.com/\S+", line)
            if m and not STATE["login_url"]:
                STATE["login_url"] = m.group(0)
                print("Tailscale login link:", STATE["login_url"])
                try:
                    os.startfile(STATE["login_url"])   # default browser mein khol do
                except Exception:
                    pass

    threading.Thread(target=reader, daemon=True).start()
    try:
        p.wait(timeout=300)
    except subprocess.TimeoutExpired:
        p.kill()
        print("Tailscale login ka wait khatam.")


def install_tailscale():
    ts = find_tailscale()
    if not ts:
        print("Tailscale install ho raha hai...")
        if shutil.which("winget"):
            run(["winget", "install", "-e", "--id", "Tailscale.Tailscale", "--silent",
                 "--accept-package-agreements", "--accept-source-agreements"])
            ts = find_tailscale()
        if not ts:
            for url in TAILSCALE_URLS:
                try:
                    path = os.path.join(tempfile.mkdtemp(), url.rsplit("/", 1)[1])
                    with open(path, "wb") as f:
                        f.write(http_get(url, 300))
                    if path.endswith(".msi"):
                        run(["msiexec", "/i", path, "/qn", "/norestart"])
                    else:
                        run([path, "/quiet"])
                    time.sleep(8)
                    ts = find_tailscale()
                    if ts:
                        break
                except Exception as e:
                    print("  Tailscale download fail:", e)
        if not ts:
            print("Tailscale auto install nahi hua. tailscale.com/download se khud install karein.")
            return None
    ip = tailscale_ip(ts)
    if not ip:
        print("Tailscale login ho raha hai...")
        tailscale_login(ts)
        for _ in range(10):
            ip = tailscale_ip(ts)
            if ip:
                break
            time.sleep(3)
    run([ts, "set", "--unattended"])   # Windows se logout ho tab bhi Tailscale chalta rahe
    return ip


# ---------------------------------------------------------------- AutoFix
def install_autofix():
    """Har boot par: ini/RDP settings check karti hai. Windows update ke baad khud theek kar deti hai."""
    os.makedirs(INSTALL_DIR, exist_ok=True)
    if FROZEN:
        dst = os.path.join(INSTALL_DIR, "RDPSetup.exe")
        if os.path.normcase(os.path.abspath(sys.executable)) != os.path.normcase(dst):
            shutil.copyfile(sys.executable, dst)
        tr = f'"{dst}" --auto'
    else:
        if "windowsapps" in sys.executable.lower():
            print("AutoFix task skip: Microsoft Store wala Python SYSTEM se nahi chal sakta.")
            return False
        dst = os.path.join(INSTALL_DIR, "bgpc.py")
        src = os.path.abspath(__file__)
        if os.path.normcase(src) != os.path.normcase(dst):
            shutil.copyfile(src, dst)
        exe = sys.executable
        if exe.lower().endswith("pythonw.exe"):
            exe = exe[:-len("pythonw.exe")] + "python.exe"
        tr = f'"{exe}" "{dst}" --auto'
    r = run(["schtasks", "/create", "/tn", TASK_NAME, "/tr", tr, "/sc", "onstart",
             "/delay", "0002:00", "/ru", "SYSTEM", "/rl", "HIGHEST", "/f"])
    if r.returncode == 0:
        print("AutoFix task lag gayi (har boot par khud check hoga).")
        return True
    print("AutoFix task nahi bani:", (r.stdout + r.stderr).strip())
    return False


def autofix_exists():
    return run(["schtasks", "/query", "/tn", TASK_NAME]).returncode == 0


# ------------------------------------------------------------------- info
def lan_ip():
    out = ps("(Get-NetIPConfiguration | Where-Object {$_.IPv4DefaultGateway -ne $null -and "
             "$_.NetAdapter.Status -eq 'Up'} | Select-Object -First 1).IPv4Address.IPAddress")
    ip = out.splitlines()[0].strip() if out else None
    if ip and not ip.startswith("169.254"):
        return ip
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return None if ip.startswith("169.254") else ip
    except Exception:
        return None


def port_open():
    try:
        with socket.create_connection(("127.0.0.1", 3389), timeout=3):
            return True
    except OSError:
        return False


def write_result(lines):
    txt = "\r\n".join(lines) + "\r\n"
    name = f"RDP_RESULT_{os.environ.get('COMPUTERNAME', 'PC')}.txt"
    for d in (BASE_DIR, INSTALL_DIR):
        try:
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, name), "w", encoding="utf-8") as f:
                f.write(txt)
        except OSError:
            pass


# ------------------------------------------------------------------- main
def main_auto():
    """Har boot par SYSTEM account se (koi UAC nahi) chalta hai. Har function
    pehle hi check karta hai ke kaam ho chuka hai ya nahi, isliye ye roz chalne
    ke liye bhi safe/fast hai, aur Windows update ke baad khud theek kar deta hai."""
    ver = termsrv_version()
    print("termsrv.dll:", ver)
    enable_rdp()
    no_sleep()
    create_friend()                 # user/password/admin, agar missing/badla ho to theek
    ok = install_wrapper(ver)        # RDP Wrapper + ini, sirf zaroorat par update
    ts_ip = install_tailscale()      # authkey se silent login, pehle se connected ho to turant return
    print(f"Version supported: {ok} | friend admin: {friend_is_admin()} | Tailscale IP: {ts_ip}")


def main():
    if os.name != "nt":
        sys.exit("Ye script sirf Windows ke liye hai.")
    ensure_admin()
    load_config()
    if SILENT:
        setup_logging()
    if AUTO:
        return main_auto()

    edition = ps("(Get-CimInstance Win32_OperatingSystem).Caption")
    ver = termsrv_version()
    print("Windows:", edition)
    print("termsrv.dll version:", ver or "pata nahi chala")

    enable_rdp()
    no_sleep()
    create_friend()
    ini_ok = install_wrapper(ver)
    ts_ip = install_tailscale()
    autofix = install_autofix() or autofix_exists()
    restart_termservice()

    time.sleep(3)
    svc = ps("(Get-Service TermService).Status")
    listening = port_open()
    ip = lan_ip()
    admin_ok = friend_is_admin()
    pc = os.environ.get("COMPUTERNAME", "PC")

    lines = [
        f"PC name           : {pc}",
        f"Windows           : {edition}",
        f"termsrv.dll       : {ver}",
        f"TermService       : {svc}",
        f"Port 3389 sun raha: {'HAAN' if listening else 'NAHI (restart ke baad dobara check karein)'}",
        f"Version supported : {'HAAN' if ini_ok else 'NAHI/PATA NAHI'}",
        f"{FRIEND} admin hai: {'HAAN' if admin_ok else 'NAHI'}",
        f"AutoFix task      : {'HAAN' if autofix else 'NAHI'}",
        "",
        "Dost ke liye (Remote Desktop app):",
        f"  PC name : {ts_ip or 'Tailscale login nahi hua - Tailscale app mein login karein'}",
        f"  User    : {FRIEND}",
        (f"  Password: {STATE['generated_pw']}   (khud ban gaya, isay dost ko de dein)"
         if STATE.get("generated_pw") else "  Password: jo config.ini mein / aapne set kiya"),
        f"  (Ghar ke WiFi par: {ip or 'LAN IP nahi mila'})",
        "",
        "PC ko EK BAAR RESTART karein.",
    ]
    if STATE["login_url"] and not ts_ip:
        lines.append(f"Tailscale login link: {STATE['login_url']}")
    write_result(lines)
    print("\n".join(lines))

    short = (f"Setup poora.\nPC: {pc}\nTailscale IP: {ts_ip or 'login baqi hai'}\n"
             f"User: {FRIEND}\nVersion supported: {'HAAN' if ini_ok else 'NAHI'}\n\n"
             "PC ko ek baar restart karein.\nTafseel: RDP_RESULT file (USB par).")
    if SILENT:
        popup(short)
        if CFG["auto_restart"]:
            run(["shutdown", "/r", "/t", "60", "/c", "RDP setup mukammal, PC restart hoga"])
    else:
        if input("\nPC abhi restart karein? (y/N): ").strip().lower() == "y":
            run(["shutdown", "/r", "/t", "15"])
            print("15 second mein restart hoga.")
        else:
            input("Restart baad mein zaroor karein. Enter dabayein...")


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        if e.code not in (0, None) and not SILENT:
            print(e.code)
            input("Enter dabayein...")
        raise
    except Exception as e:
        print("Error:", repr(e))
        if SILENT:
            popup(f"Setup fail hua:\n{e}\n\nLog: {os.path.join(INSTALL_DIR, 'setup.log')}", error=True)
        else:
            input("Enter dabayein...")
