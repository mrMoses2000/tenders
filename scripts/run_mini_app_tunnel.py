"""Publish the Mini App through a temporary Cloudflare Tunnel.

The tunnel hostname changes on restart. Keep Telegram's menu URL in sync only
after the new public endpoint answers its health check.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from threading import Event, Thread, Timer

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"
CLOUDFLARED = Path(os.environ.get("CLOUDFLARED_BIN", Path.home() / ".local/bin/cloudflared"))
URL_PATTERN = re.compile(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com")
ENV_PATTERN = re.compile(r"^MINI_APP_PUBLIC_URL=.*$", re.MULTILINE)
LOGGER = logging.getLogger("procurement-mini-app-tunnel")
STARTUP_TIMEOUT_SECONDS = 120


def check_public_health(public_url: str) -> bool:
    hostname = public_url.removeprefix("https://")
    health_url = f"{public_url}/api/health"
    # The host can reach Cloudflare through its configured HTTPS proxy even
    # when direct UDP DNS queries to 1.1.1.1 are blocked.
    if _curl_health(health_url):
        return True
    # The host's resolver may cache an initial NXDOMAIN for thirty minutes.
    # Use a public resolver and retain normal TLS certificate verification.
    try:
        lookup = subprocess.run(  # noqa: S603 - hostname matches Cloudflare's URL pattern
            ["/usr/bin/dig", "+short", hostname, "@1.1.1.1"],
            capture_output=True, text=True, check=False, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if lookup.returncode != 0:
        return False
    for value in lookup.stdout.splitlines():
        try:
            address = ipaddress.IPv4Address(value.strip())
        except ipaddress.AddressValueError:
            continue
        if _curl_health(health_url, address=str(address), hostname=hostname):
            return True
    return False


def _curl_health(url: str, *, address: str | None = None,
                 hostname: str | None = None) -> bool:
    command = ["/usr/bin/curl", "--fail", "--silent", "--show-error",
               "--max-time", "8"]
    if address is not None and hostname is not None:
        command.extend(["--resolve", f"{hostname}:443:{address}"])
    command.append(url)
    try:
        checked = subprocess.run(  # noqa: S603 - URL and optional IP are validated
            command, capture_output=True, text=True, check=False, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if checked.returncode != 0:
        return False
    try:
        return json.loads(checked.stdout) == {"status": "ok"}
    except json.JSONDecodeError:
        return False


def wait_for_health(public_url: str) -> bool:
    for _ in range(20):
        if check_public_health(public_url):
            return True
        time.sleep(2)
    return False


def monitor_health(process: subprocess.Popen[str], public_url: str, stop: Event) -> None:
    failures = 0
    while not stop.wait(30):
        if process.poll() is not None:
            return
        if check_public_health(public_url):
            failures = 0
            continue
        failures += 1
        LOGGER.warning("Public Mini App health check failed (%s/3): %s", failures, public_url)
        if failures == 3:
            LOGGER.error("Restarting unavailable Mini App tunnel")
            process.terminate()
            return


def sync_public_url(public_url: str) -> None:
    content = ENV_FILE.read_text(encoding="utf-8")
    replacement = f"MINI_APP_PUBLIC_URL={public_url}/"
    if ENV_PATTERN.search(content):
        updated = ENV_PATTERN.sub(replacement, content)
    else:
        updated = content.rstrip("\n") + "\n" + replacement + "\n"
    if updated == content:
        LOGGER.info("Public URL is unchanged")
        return
    temporary = ENV_FILE.with_name(".env.tunnel.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(updated)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        temporary.replace(ENV_FILE)
    finally:
        temporary.unlink(missing_ok=True)
    LOGGER.info("Updated Mini App URL to %s", public_url)
    subprocess.run(
        ["/usr/bin/systemctl", "--user", "restart", "procurement-agent-bot.service"],
        check=True, timeout=30,
    )
    LOGGER.info("Telegram bot restarted with the new menu URL")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not CLOUDFLARED.is_file() or not os.access(CLOUDFLARED, os.X_OK):
        raise RuntimeError("cloudflared is missing or not executable")
    process = subprocess.Popen(  # noqa: S603 - executable is checked above
        [str(CLOUDFLARED), "tunnel", "--url", "http://127.0.0.1:8082",
         "--protocol", "http2", "--edge-ip-version", "4", "--no-autoupdate"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
    )

    def terminate(_signum: int, _frame: object) -> None:
        process.terminate()

    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    monitor_stop = Event()
    monitor: Thread | None = None
    ready = Event()

    def stop_stalled_startup() -> None:
        if not ready.is_set() and process.poll() is None:
            LOGGER.error("Mini App tunnel did not become publicly healthy within %s seconds",
                         STARTUP_TIMEOUT_SECONDS)
            process.terminate()

    startup_watchdog = Timer(STARTUP_TIMEOUT_SECONDS, stop_stalled_startup)
    startup_watchdog.daemon = True
    startup_watchdog.start()
    try:
        assert process.stdout is not None
        for line in process.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            match = URL_PATTERN.search(line)
            if match:
                public_url = match.group(0)
                monitor_stop.set()
                if monitor is not None:
                    monitor.join(timeout=15)
                monitor_stop = Event()
                if wait_for_health(public_url):
                    sync_public_url(public_url)
                    ready.set()
                    monitor = Thread(
                        target=monitor_health,
                        args=(process, public_url, monitor_stop),
                        daemon=True,
                    )
                    monitor.start()
                else:
                    LOGGER.warning("Public Mini App health check failed at %s", public_url)
                    process.terminate()
        return process.wait() or 1
    finally:
        startup_watchdog.cancel()
        monitor_stop.set()
        if monitor is not None:
            monitor.join(timeout=15)
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()


if __name__ == "__main__":
    raise SystemExit(main())
