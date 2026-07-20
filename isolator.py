import os
import sys
import shutil
import zipfile
import tarfile
import urllib.request
import subprocess
import argparse
import time
from pathlib import Path

if os.name == 'nt':
    import msvcrt
else:
    import select
    import termios
    import tty

STANDALONE_RELEASE = "20260623"

def print_status(msg: str, status="INFO"):
    colors = {"INFO": "\033[94m", "SUCCESS": "\033[92m", "WARN": "\033[93m", "ERROR": "\033[91m", "RESET": "\033[0m"}
    if os.name == 'nt' and not os.environ.get("WT_SESSION"):
        print(f"[{status}] {msg}")
    else:
        print(f"{colors.get(status, '')}[{status}] {msg}{colors['RESET']}")

def scrub_environment() -> dict:
    clean_env = os.environ.copy()
    for var in ["PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "PYTHONCASEOK", "PIL_PATH"]:
        clean_env.pop(var, None)
    clean_env["PYTHONUTF8"] = "1"
    clean_env["PYTHONIOENCODING"] = "utf-8"
    return clean_env

def fetch_runtime(version: str, target_dir: Path) -> Path:
    executable = target_dir / ("python.exe" if os.name == 'nt' else "bin/python")
    if executable.exists():
        return executable
        
    if target_dir.exists():
        shutil.rmtree(target_dir, ignore_errors=True)
        
    target_dir.mkdir(parents=True, exist_ok=True)
    isolated_env = scrub_environment()

    if os.name == 'nt':
        archive_name = f"python-{version}-embed-amd64.zip"
        url = f"https://www.python.org/ftp/python/{version}/{archive_name}"
        archive_path = target_dir / archive_name

        print_status(f"Streaming standalone Windows execution layer ({version})...")
        ctx = urllib.request.Request(url, headers={"User-Agent": "WebIsolator-Core"})
        
        with urllib.request.urlopen(ctx, timeout=30) as response, open(archive_path, "wb") as out_file:
            while chunk := response.read(65536):
                out_file.write(chunk)

        with zipfile.ZipFile(archive_path, 'r') as zip_ref:
            zip_ref.extractall(target_dir)
        archive_path.unlink()

        for pth_file in target_dir.glob("*._pth"):
            orig_lines = pth_file.read_text(encoding="utf-8").splitlines()
            core_zips = [line.strip() for line in orig_lines if line.strip().endswith(".zip")]
            payload_paths = core_zips + [".", "Lib/site-packages", "import site"]
            pth_file.write_text("\n".join(payload_paths) + "\n", encoding="utf-8")
    else:
        arch = "x86_64" if sys.maxsize > 2**32 else "aarch64"
        triple = f"{arch}-unknown-linux-gnu" if sys.platform.startswith("linux") else f"{arch}-apple-darwin"
        archive_name = f"cpython-{version}+{STANDALONE_RELEASE}-{triple}-install_only.tar.gz"
        url = f"https://github.com/astral-sh/python-build-standalone/releases/download/{STANDALONE_RELEASE}/{archive_name}"
        archive_path = target_dir / archive_name

        print_status(f"Streaming standalone POSIX execution layer ({version})...")
        ctx = urllib.request.Request(url, headers={"User-Agent": "WebIsolator-Core"})
        
        with urllib.request.urlopen(ctx, timeout=30) as response, open(archive_path, "wb") as out_file:
            while chunk := response.read(65536):
                out_file.write(chunk)

        with tarfile.open(archive_path, "r:gz") as tar_ref:
            tar_ref.extractall(target_dir)
        archive_path.unlink()
        
        source_extracted = target_dir / "python"
        if source_extracted.exists():
            for item in source_extracted.iterdir():
                shutil.move(str(item), str(target_dir / item.name))
            shutil.rmtree(source_extracted, ignore_errors=True)

    pip_bootstrapper = target_dir / "get-pip.py"
    v_parts = version.split('.')
    if len(v_parts) >= 2 and v_parts[0] == '3' and v_parts[1] in ['6', '7', '8', '9']:
        pip_url = f"https://bootstrap.pypa.io/pip/{v_parts[0]}.{v_parts[1]}/get-pip.py"
    else:
        pip_url = "https://bootstrap.pypa.io/get-pip.py"
    
    try:
        pip_ctx = urllib.request.Request(pip_url, headers={"User-Agent": "WebIsolator-Core"})
        with urllib.request.urlopen(pip_ctx, timeout=30) as response, open(pip_bootstrapper, "wb") as out_file:
            while chunk := response.read(65536):
                out_file.write(chunk)
                
        subprocess.run([str(executable), "-I", str(pip_bootstrapper), "--no-warn-script-location"], env=isolated_env, check=True)
    finally:
        if pip_bootstrapper.exists():
            pip_bootstrapper.unlink()
    
    return executable

def parse_manifest(manifest_path: Path) -> list:
    apps = []
    if not manifest_path.exists():
        manifest_path.write_text("# Name | Script Path | Version | Footprint Root\n", encoding="utf-8")
        return apps
        
    for line in manifest_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 4:
            apps.append({
                "name": parts[0],
                "script": Path(parts[1]),
                "version": parts[2],
                "root": Path(parts[3]),
                "proc": None,
                "log_file": None
            })
    return apps

def converge_dependencies(executable: Path, app_root: Path):
    req_file = app_root / "requirements.txt"
    if req_file.exists():
        isolated_env = scrub_environment()
        try:
            subprocess.run([
                str(executable), "-m", "pip", "install", 
                "-r", str(req_file), "--upgrade", "--no-warn-script-location"
            ], env=isolated_env, capture_output=True, check=True)
        except subprocess.CalledProcessError:
            pass

def enforce_windows_sandbox(app_root: Path, central_executable: Path) -> Path:
    if os.name != 'nt':
        return central_executable
        
    try:
        sandbox_bin = app_root / ".tmp" / "bin"
        if sandbox_bin.exists():
            shutil.rmtree(sandbox_bin, ignore_errors=True)
        sandbox_bin.mkdir(parents=True, exist_ok=True)
        
        for dll_file in central_executable.parent.glob("*.dll"):
            shutil.copy(str(dll_file), str(sandbox_bin / dll_file.name))
        local_exe = sandbox_bin / "python.exe"
        shutil.copy(str(central_executable), str(local_exe))
        
        relative_runtime_root = os.path.relpath(central_executable.parent, sandbox_bin)
        
        pth_paths = []
        for central_zip in central_executable.parent.glob("*.zip"):
            pth_paths.append(os.path.join(relative_runtime_root, central_zip.name))
        pth_paths.append(relative_runtime_root)
        pth_paths.append(os.path.join(relative_runtime_root, "Lib", "site-packages"))
        pth_paths.append(".")
        pth_paths.append("import site")
        
        for pth_file in central_executable.parent.glob("*._pth"):
            (sandbox_bin / pth_file.name).write_text("\n".join(pth_paths) + "\n", encoding="utf-8")
            
        subprocess.run(["icacls", str(app_root), "/setintegritylevel", "Low", "/T", "/C", "/Q"], capture_output=True, check=True)
        subprocess.run(["icacls", str(local_exe), "/setintegritylevel", "Low"], capture_output=True, check=True)
        
        return local_exe
    except Exception:
        return central_executable

def spawn_silo(app: dict, runtimes_dir: Path):
    if app["proc"] and app["proc"].poll() is None:
        return
        
    version_str = app["version"].strip()
    target_runtime = runtimes_dir / f"{'win' if os.name == 'nt' else 'posix'}_{version_str.replace('.', '_')}"
    executable = fetch_runtime(version_str, target_runtime)
    
    converge_dependencies(executable, app["root"])
    target_executable = enforce_windows_sandbox(app["root"], executable)
    
    isolated_env = scrub_environment()
    tmp_dir = app["root"] / ".tmp"
    tmp_dir.mkdir(exist_ok=True)
    
    isolated_env["TEMP"] = str(tmp_dir)
    isolated_env["TMP"] = str(tmp_dir)
    isolated_env["PYTHONHOME"] = str(executable.parent)
    
    cmd = [str(target_executable), "-I", str(app["script"])]
    log_path = tmp_dir / "output.log"
    
    if app["log_file"]:
        app["log_file"].close()
    app["log_file"] = open(log_path, "a", encoding="utf-8", buffering=1)
    
    if os.name == 'nt':
        CREATE_NO_WINDOW = 0x08000000
        app["proc"] = subprocess.Popen(
            cmd, 
            cwd=str(app["root"]), 
            env=isolated_env, 
            stdout=app["log_file"],
            stderr=app["log_file"],
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
        )
    else:
        def setup_posix_namespaces():
            if os.getuid() == 0:
                try:
                    if hasattr(os, 'unshare'):
                        os.unshare(0x20000000 | 0x00020000)
                except Exception:
                    pass
                os.setgroups([])
                os.setgid(65534)
                os.setuid(65534)
        
        app["proc"] = subprocess.Popen(
            cmd, 
            cwd=str(app["root"]), 
            env=isolated_env, 
            stdout=app["log_file"],
            stderr=app["log_file"],
            preexec_fn=setup_posix_namespaces
        )

def kill_silo(app: dict):
    if app["proc"]:
        if app["proc"].poll() is None:
            if os.name == 'nt':
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(app["proc"].pid)], capture_output=True)
            else:
                app["proc"].kill()
            app["proc"].wait()
        app["proc"] = None
    if app["log_file"]:
        app["log_file"].close()
        app["log_file"] = None

def get_key_stroke(timeout: float):
    if os.name == 'nt':
        start_time = time.time()
        while (time.time() - start_time) < timeout:
            if msvcrt.kbhit():
                ch = msvcrt.getch()
                if ch == b'\x1b': 
                    return "esc"
                if ch in (b'\x00', b'\xe0'):
                    ch2 = msvcrt.getch()
                    if ch2 == b'H': return "UP"
                    if ch2 == b'P': return "DOWN"
                if ch in (b'\r', b'\n'): return "ENTER"
                try:
                    return ch.decode('utf-8').lower()
                except Exception:
                    return None
            time.sleep(0.01)
        return None
    else:
        r, _, _ = select.select([sys.stdin], [], [], timeout)
        if r:
            ch = sys.stdin.read(1)
            if ch == '\x1b':
                r2, _, _ = select.select([sys.stdin], [], [], 0.05)
                if r2:
                    ch2 = sys.stdin.read(2)
                    if ch2 == '[A': return "UP"
                    if ch2 == '[B': return "DOWN"
                else:
                    return "esc"
            if ch in ('\r', '\n'): return "ENTER"
            return ch.lower()
        return None

def read_log_tail(path: Path, max_lines=20) -> list:
    if not path.exists():
        return []
    bytes_needed = max_lines * 256
    with open(path, "rb") as f:
        try:
            f.seek(0, os.SEEK_END)
            file_size = f.tell()
            seek_pos = max(0, file_size - bytes_needed)
            f.seek(seek_pos, os.SEEK_SET)
            raw_data = f.read()
            lines = raw_data.decode('utf-8', errors='ignore').splitlines()
            return lines[-max_lines:] if len(lines) > max_lines else lines
        except Exception:
            return []

def draw_tui(apps: list, selected_idx: int, active_view: str):
    os.system('cls' if os.name == 'nt' else 'clear')
    print("\033[95m=========================================================================\033[0m")
    print("    WebIsolator Core - Production Persistent Management Dashboard       ")
    print("\033[95m=========================================================================\033[0m\n")
    
    if active_view == "MAIN":
        print(f"{'Service Identity':<15} | {'PID':<8} | {'Version':<8} | {'Operational Status':<12}")
        print("-" * 73)
        for idx, app in enumerate(apps):
            status_str = "STOPPED"
            pid_str = "N/A"
            color = "\033[91m"
            
            if app["proc"]:
                poll = app["proc"].poll()
                if poll is None:
                    status_str = "RUNNING"
                    pid_str = str(app["proc"].pid)
                    color = "\033[92m"
                elif poll == 0:
                    status_str = "STOPPED"
                else:
                    status_str = f"ERROR ({poll})"
            
            prefix = " > " if idx == selected_idx else "   "
            name_part = f"{prefix}{app['name']}"
            status_color_end = "\033[44m" if idx == selected_idx else "\033[0m"
            line = f"{name_part:<15} | {pid_str:<8} | {app['version']:<8} | {color}{status_str:<12}{status_color_end}"
            
            if idx == selected_idx:
                print(f"\033[44m{line}\033[0m")
            else:
                print(line)
        print("\n\033[93mCommands: [UP/DOWN] Navigate | [ENTER/R] Relaunch Silo | [K] Kill Silo | [L] View Logs | [Q] Exit\033[0m")
    
    elif active_view == "LOGS":
        app = apps[selected_idx]
        print(f"\033[96mDiagnostics tail boundary for service: [{app['name']}]\033[0m")
        print("-" * 73)
        log_path = app["root"] / ".tmp" / "output.log"
        lines = read_log_tail(log_path, max_lines=20)
        if lines:
            for line in lines:
                print(line)
        else:
            print("[INFO] Execution stream log empty or initialization pending.")
        print("\n\033[93mCommands: [Esc/L] Return to Dashboard Context\033[0m")

def main():
    parser = argparse.ArgumentParser(description="WebIsolator Launcher Engine")
    parser.add_argument("--start", action="store_true", help="Boot isolation runtime panel")
    args = parser.parse_args()
    
    if not args.start:
        parser.print_help()
        sys.exit(0)
        
    engine_root = Path(__file__).parent.resolve()
    manifest_file = engine_root / "apps.txt"
    runtimes_dir = engine_root / "runtimes"
    
    apps = parse_manifest(manifest_file)
    if not apps:
        print_status("Manifest configuration trace empty.", "ERROR")
        sys.exit(1)
        
    print_status("Initializing structural network backend cluster bounds...")
    for app in apps:
        try:
            spawn_silo(app, runtimes_dir)
        except Exception as e:
            print_status(f"Initial allocation bound fractured for {app['name']}: {e}", "ERROR")

    selected_idx = 0
    active_view = "MAIN"
    force_redraw = True
    last_refresh_time = 0.0
    PASSIVE_REFRESH_INTERVAL = 2.0
    
    old_settings = None
    if os.name != 'nt':
        old_settings = termios.tcgetattr(sys.stdin)
        tty.setcbreak(sys.stdin.fileno())
        
    try:
        while True:
            now = time.time()
            if force_redraw or (now - last_refresh_time >= PASSIVE_REFRESH_INTERVAL):
                draw_tui(apps, selected_idx, active_view)
                last_refresh_time = now
                force_redraw = False
            
            key = get_key_stroke(timeout=0.1)
            if not key:
                continue
                
            force_redraw = True
                
            if active_view == "MAIN":
                if key == "UP":
                    selected_idx = (selected_idx - 1) % len(apps)
                elif key == "DOWN":
                    selected_idx = (selected_idx + 1) % len(apps)
                elif key in ("enter", "r"):
                    print_status(f"Re-converging and spawning workspace boundary: [{apps[selected_idx]['name']}]", "WARN")
                    kill_silo(apps[selected_idx])
                    spawn_silo(apps[selected_idx], runtimes_dir)
                elif key == "k":
                    print_status(f"Terminating boundary context: [{apps[selected_idx]['name']}]", "WARN")
                    kill_silo(apps[selected_idx])
                elif key == "l":
                    active_view = "LOGS"
                elif key == "q":
                    break
            elif active_view == "LOGS":
                if key in ("l", "esc"):
                    active_view = "MAIN"
                    
    finally:
        if old_settings:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
        os.system('cls' if os.name == 'nt' else 'clear')
        print_status("De-allocating process trees and teardown configuration sequence...")
        for app in apps:
            kill_silo(app)
        print_status("All process boundaries torn down cleanly.", "SUCCESS")

if __name__ == "__main__":
    main()