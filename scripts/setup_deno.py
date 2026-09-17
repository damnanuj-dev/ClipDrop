#!/usr/bin/env python3
"""
ClipDrop Deno Installer & Verifier
Downloads, extracts, and validates a verified Deno executable for yt-dlp EJS challenges.
Supports Linux (Vercel deployment) and Windows/macOS (local development).
"""

import os
import sys
import platform
import zipfile
import io
import subprocess
import argparse
from typing import Optional

try:
    import requests
except ImportError:
    import urllib.request as urllib_request
    requests = None

DENO_DEFAULT_VERSION = "2.9.7"

def get_platform_asset(target_os: Optional[str] = None) -> tuple[str, str]:
    """Returns (asset_filename, binary_name) based on target OS."""
    os_name = (target_os or platform.system()).lower()
    
    if "linux" in os_name:
        return "deno-x86_64-unknown-linux-gnu.zip", "deno"
    elif "windows" in os_name:
        return "deno-x86_64-pc-windows-msvc.zip", "deno.exe"
    elif "darwin" in os_name:
        arch = platform.machine().lower()
        if "arm" in arch or "aarch" in arch:
            return "deno-aarch64-apple-darwin.zip", "deno"
        return "deno-x86_64-apple-darwin.zip", "deno"
    else:
        # Fallback to linux x86_64 (Vercel environment)
        return "deno-x86_64-unknown-linux-gnu.zip", "deno"

def download_bytes(url: str) -> bytes:
    """Download bytes using requests or urllib."""
    headers = {"User-Agent": "ClipDrop-Setup/1.0"}
    if requests is not None:
        resp = requests.get(url, headers=headers, timeout=60)
        resp.raise_for_status()
        return resp.content
    else:
        req = urllib_request.Request(url, headers=headers)
        with urllib_request.urlopen(req, timeout=60) as resp:
            return resp.read()

def install_deno(output_dir: str, version: str = DENO_DEFAULT_VERSION, target_os: Optional[str] = None) -> str:
    """Installs Deno into output_dir and returns absolute path to executable."""
    os.makedirs(output_dir, exist_ok=True)
    asset_name, binary_name = get_platform_asset(target_os)
    target_path = os.path.abspath(os.path.join(output_dir, binary_name))

    # If already exists and works, check version
    if os.path.exists(target_path) and os.path.isfile(target_path):
        try:
            if not os.access(target_path, os.X_OK):
                os.chmod(target_path, 0o755)
            res = subprocess.run([target_path, "--version"], capture_output=True, text=True, timeout=10)
            if res.returncode == 0:
                print(f"[setup_deno] Existing Deno binary valid at {target_path}: {res.stdout.splitlines()[0]}")
                return target_path
        except Exception as e:
            print(f"[setup_deno] Existing binary failed check ({e}), re-downloading...")

    url = f"https://github.com/denoland/deno/releases/download/v{version}/{asset_name}"
    print(f"[setup_deno] Downloading Deno v{version} from {url}...")
    content = download_bytes(url)

    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        # Find the executable in archive
        matched_member = None
        for member in zf.namelist():
            if os.path.basename(member) in ("deno", "deno.exe"):
                matched_member = member
                break

        if not matched_member:
            raise RuntimeError(f"Archive {asset_name} did not contain a 'deno' executable. Found: {zf.namelist()}")

        with open(target_path, "wb") as f:
            f.write(zf.read(matched_member))

    if os.name != "nt":
        os.chmod(target_path, 0o755)

    # Validate the downloaded binary if runnable on current host OS
    is_host = (target_os is None) or (target_os.lower() in platform.system().lower())
    if is_host:
        res = subprocess.run([target_path, "--version"], capture_output=True, text=True, timeout=10)
        if res.returncode != 0:
            raise RuntimeError(f"Deno execution test failed with exit code {res.returncode}: {res.stderr}")
        print(f"[setup_deno] Deno installed and verified successfully: {res.stdout.splitlines()[0]}")
    else:
        print(f"[setup_deno] Cross-compiled Deno binary written to {target_path} for target '{target_os}'.")

    return target_path

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Install and verify Deno binary for ClipDrop.")
    parser.add_argument("--output-dir", default=os.path.join(os.path.dirname(__file__), "..", "bin"), help="Output directory")
    parser.add_argument("--version", default=DENO_DEFAULT_VERSION, help="Deno release version")
    parser.add_argument("--target-os", default=None, help="Target OS: linux, windows, darwin (defaults to current)")
    args = parser.parse_args()

    try:
        path = install_deno(args.output_dir, args.version, args.target_os)
        print(f"[setup_deno] SUCCESS: Deno ready at {path}")
        sys.exit(0)
    except Exception as exc:
        print(f"[setup_deno] ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
