"""
用 Python 启动带代理 + 代理认证扩展的 Chromium，参数与 ChromiumForWisconsin 一致。
用法: uv run python CDPDemo/launch_chromium.py
启动后 CDP 端口 9222，可再运行 cdp_demo 等连接。
"""

import platform
import socket
import subprocess
import sys
from pathlib import Path
from typing import Optional
from time import sleep

CHROMIUM_BIN = "/Applications/Chromium.app/Contents/MacOS/Chromium"
PROFILE_ID = "wisconsin-caiwu"
PROXY_HOST = "sg.arxlabs.io:3010"
TIMEZONE = "America/Chicago"
FINGERPRINT_ID = "4567"
REMOTE_DEBUGGING_PORT = 9222


def check_port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def kill_process_on_port(port: int) -> bool:
    """
    在常见桌面系统上（Windows / macOS / Linux）尝试找到占用指定端口的进程并强制结束。
    返回是否成功找到并发送结束信号。
    """
    system = platform.system()

    # Windows: 用 netstat 找 PID，再用 taskkill 结束
    if system == "Windows":
        try:
            result = subprocess.run(
                ["netstat", "-ano"],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            if result.stdout is None:
                return False
            pid_set: set[str] = set()
            target = f":{port}"
            for line in result.stdout.splitlines():
                line = line.strip()
                if not line:
                    continue
                # 只看包含目标端口的行
                if target not in line:
                    continue
                parts = line.split()
                if len(parts) < 5:
                    continue
                pid = parts[-1]
                if pid.isdigit():
                    pid_set.add(pid)
            if not pid_set:
                return False
            subprocess.run(
                ["taskkill", "/F"] + [arg for pid in pid_set for arg in ("/PID", pid)],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
        except Exception:
            return False

    # 类 Unix（macOS / Linux）：优先用 lsof，其次尝试 fuser
    try:
        result = subprocess.run(
            ["lsof", "-t", f"-i:{port}"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        pids = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    except FileNotFoundError:
        pids = []

    if not pids:
        # lsof 不可用或没找到，尝试 fuser（常见于 Linux）
        try:
            result = subprocess.run(
                ["fuser", f"{port}/tcp"],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            output = (result.stdout or "").strip()
            if output:
                pids = [pid for pid in output.split() if pid.isdigit()]
        except FileNotFoundError:
            pids = []

    if not pids:
        return False

    subprocess.run(
        ["kill", "-9", *pids],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return True


def launch_browser(
    remote_debugging_port: int = REMOTE_DEBUGGING_PORT,
    proxy_host: str = PROXY_HOST,
    timezone: str = TIMEZONE,
    fingerprint_id: str = FINGERPRINT_ID,
    fingerprint_platform: str = "windows",
    fingerprint_brand: str = "Edge",
) -> tuple[Optional[subprocess.Popen], Path]:
    """启动 Chromium，返回 (进程对象, user_data_dir)；失败返回 (None, user_data_dir)。"""

    user_data_dir = Path.home() / "fp-data" / fingerprint_id
    extension_path = (
        Path(__file__).resolve().parent / "proxy-auth-extension" / "fingerprint_id"
    )

    # 检查调试端口是否被占用，如果被占用则尝试释放
    if not check_port_available(remote_debugging_port):
        print(f"端口 {remote_debugging_port} 被占用，尝试释放...", file=sys.stderr)
        if not kill_process_on_port(remote_debugging_port):
            print(
                f"无法自动释放端口 {remote_debugging_port}，请手动检查并结束相关进程。",
                file=sys.stderr,
            )
            return None, user_data_dir
    sleep(2)

    if not Path(CHROMIUM_BIN).exists():
        print(f"Chromium 不存在: {CHROMIUM_BIN}", file=sys.stderr)
        return None, user_data_dir
    if not extension_path.is_dir():
        print(f"扩展目录不存在: {extension_path}", file=sys.stderr)
        return None, user_data_dir

    args = [
        CHROMIUM_BIN,
        f"--remote-debugging-port={remote_debugging_port}",
        f"--load-extension={extension_path}",
        f"--fingerprint={fingerprint_id}",
        f"--user-data-dir={user_data_dir}",
        f"--timezone={timezone}",
        f"--proxy-server=http://{proxy_host}",
        f"--fingerprint-platform={fingerprint_platform}",
        f"--fingerprint-brand={fingerprint_brand}",
        "--force-webrtc-ip-handling-policy",
        "--webrtc-ip-handling-policy=disable_non_proxied_udp",
        "--disable-features=AsyncDNS",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    proc = subprocess.Popen(
        args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return proc, user_data_dir


def main() -> int:
    proc, user_data_dir = launch_browser(fingerprint_id="4567")
    if proc is None:
        return 1
    print(
        f"已启动 Chromium PID={proc.pid}，CDP 端口 {REMOTE_DEBUGGING_PORT}，用户目录 {user_data_dir}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
