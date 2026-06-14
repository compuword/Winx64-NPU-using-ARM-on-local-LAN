"""
npu_watchdog.py -- Windows service that manages N ARM NPU server connections.

- Reads config/npu_connections.json for static server list
- Auto-discovers additional servers on LAN via port 7474 scan
- Monitors health of each server (HTTP /health on port 7476)
- Updates Windows registry with available server count and total compute
- Writes live state to: C:\\ProgramData\\CexNPU\\state.json
- Exposes HTTP API on port 7475 (TX port) for orchestrator queries

No limit on number of NPU servers. Each discovered server is added to the
active pool. The WDDM driver reads the aggregate utilization from shared memory.

Install as Windows Service:
  python npu_watchdog.py install
  python npu_watchdog.py start
  python npu_watchdog.py stop
  python npu_watchdog.py remove
"""

import http.server
import json
import logging
import os
import pathlib
import socket
import struct
import threading
import time
import urllib.request
import winreg
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Dict, List, Optional

log = logging.getLogger("npu_watchdog")

STATE_DIR   = pathlib.Path(os.environ.get("CEX_STATE_DIR", r"C:\ProgramData\CexNPU"))
STATE_FILE  = STATE_DIR / "state.json"
CONFIG_FILE = pathlib.Path(__file__).parent.parent / "config" / "npu_connections.json"

PORT_NPU_RX     = 7474
PORT_NPU_HEALTH = 7476
PORT_TX_API     = 7475   # watchdog HTTP API (TX port per network_config.yaml)
AUTH_KEY        = os.environ.get("CEX_NET_KEY", "")


# ---------------------------------------------------------------
# Server record
# ---------------------------------------------------------------

class NpuServer:
    def __init__(self, host: str, port: int = PORT_NPU_RX,
                 health_port: int = PORT_NPU_HEALTH, label: str = ""):
        self.id           = f"npu-{host}"
        self.host         = host
        self.port         = port
        self.health_port  = health_port
        self.label        = label or host
        self.available    = False
        self.capabilities: dict = {}
        self.latency_ms   = 9999.0
        self.last_seen:  Optional[str] = None
        self.fail_count   = 0

    def probe(self, timeout: float = 2.0) -> bool:
        try:
            url = f"http://{self.host}:{self.health_port}/health"
            with urllib.request.urlopen(url, timeout=timeout) as r:
                self.capabilities = json.loads(r.read())
            self.available  = True
            self.fail_count = 0
            self.last_seen  = datetime.now(timezone.utc).isoformat()
            return True
        except Exception:
            self.fail_count += 1
            if self.fail_count >= 3:
                self.available = False
            return False

    def to_dict(self) -> dict:
        return {
            "id":           self.id,
            "host":         self.host,
            "port":         self.port,
            "health_port":  self.health_port,
            "label":        self.label,
            "available":    self.available,
            "latency_ms":   round(self.latency_ms, 1),
            "last_seen":    self.last_seen,
            "capabilities": self.capabilities,
        }


# ---------------------------------------------------------------
# Server pool (no limit)
# ---------------------------------------------------------------

class NpuServerPool:
    def __init__(self):
        self._servers:  Dict[str, NpuServer] = {}
        self._lock      = threading.Lock()
        self._cfg       = self._load_config()

    def _load_config(self) -> dict:
        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception as exc:
            log.warning("Could not load %s: %s", CONFIG_FILE, exc)
            return {}

    def add(self, host: str, port: int = PORT_NPU_RX,
            health_port: int = PORT_NPU_HEALTH, label: str = "") -> NpuServer:
        srv = NpuServer(host, port, health_port, label)
        with self._lock:
            self._servers[srv.id] = srv
        log.info("Added NPU server: %s (%s)", srv.id, host)
        return srv

    def load_from_config(self):
        for entry in self._cfg.get("servers", []):
            if entry.get("enabled", True):
                self.add(
                    host        = entry["host"],
                    port        = entry.get("port", PORT_NPU_RX),
                    health_port = entry.get("health_port", PORT_NPU_HEALTH),
                    label       = entry.get("label", ""),
                )

    def discover_lan(self, subnet: Optional[str] = None, timeout: float = 0.5):
        """Scan entire subnet for NPU servers. Adds new ones to pool."""
        import ipaddress, concurrent.futures
        subnet = subnet or self._cfg.get("discovery", {}).get("scan_subnet", "192.168.1.0/24")
        net    = ipaddress.ip_network(subnet, strict=False)

        def probe_host(ip: str):
            try:
                s = socket.create_connection((ip, PORT_NPU_HEALTH), timeout=timeout)
                s.close()
                return ip
            except OSError:
                return None

        with concurrent.futures.ThreadPoolExecutor(max_workers=64) as pool:
            found = [r for r in pool.map(probe_host, [str(h) for h in net.hosts()]) if r]

        new_count = 0
        with self._lock:
            existing = set(self._servers.keys())
        for ip in found:
            sid = f"npu-{ip}"
            if sid not in existing:
                self.add(ip)
                new_count += 1

        if new_count:
            log.info("LAN discovery: found %d new NPU server(s)", new_count)
        return found

    def probe_all(self):
        with self._lock:
            servers = list(self._servers.values())
        with ThreadPoolExecutor(max_workers=16) as pool:
            pool.map(lambda s: s.probe(), servers)

    def get_available(self) -> List[NpuServer]:
        with self._lock:
            return [s for s in self._servers.values() if s.available]

    def total_compute(self) -> dict:
        available = self.get_available()
        return {
            "npu_server_count":   len(available),
            "total_npu_servers":  len(self._servers),
            "active_providers":   list({
                s.capabilities.get("providers", ["QNNExecutionProvider"])[0]
                for s in available
            }),
            "servers_online":     [s.host for s in available],
        }

    def state_snapshot(self) -> dict:
        with self._lock:
            servers = [s.to_dict() for s in self._servers.values()]
        return {
            "timestamp":     datetime.now(timezone.utc).isoformat(),
            "total_compute": self.total_compute(),
            "servers":       servers,
            "port_map": {
                "inference_rx": PORT_NPU_RX,
                "health":       PORT_NPU_HEALTH,
                "watchdog_api": PORT_TX_API,
            },
        }

    def write_state(self):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        snap = self.state_snapshot()
        STATE_FILE.write_text(json.dumps(snap, indent=2), encoding="utf-8")
        self._update_registry(snap)

    def _update_registry(self, snap: dict):
        try:
            key = winreg.CreateKey(winreg.HKEY_LOCAL_MACHINE,
                                   r"SOFTWARE\CexVirtualNPU\Pool")
            tc  = snap["total_compute"]
            winreg.SetValueEx(key, "ServerCount",  0, winreg.REG_DWORD,
                              tc["npu_server_count"])
            winreg.SetValueEx(key, "TotalServers", 0, winreg.REG_DWORD,
                              tc["total_npu_servers"])
            winreg.SetValueEx(key, "StateFile",    0, winreg.REG_SZ,
                              str(STATE_FILE))
            winreg.CloseKey(key)
        except Exception as exc:
            log.debug("Registry update skipped: %s", exc)


# ---------------------------------------------------------------
# HTTP API on port 7475 (TX port)
# ---------------------------------------------------------------

_pool: Optional[NpuServerPool] = None


class _ApiHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args): pass

    def do_GET(self):
        if self.path in ("/resources", "/state", "/"):
            body = json.dumps(_pool.state_snapshot(), indent=2).encode()
            self.send_response(200)
        elif self.path == "/compute":
            body = json.dumps(_pool.total_compute(), indent=2).encode()
            self.send_response(200)
        elif self.path == "/health":
            body = b'{"status":"ok"}'
            self.send_response(200)
        else:
            body = b'{"error":"not found"}'
            self.send_response(404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _run_api(port: int = PORT_TX_API):
    srv = http.server.HTTPServer(("0.0.0.0", port), _ApiHandler)
    log.info("Watchdog API: http://0.0.0.0:%d/resources", port)
    srv.serve_forever()


# ---------------------------------------------------------------
# Main watchdog loop
# ---------------------------------------------------------------

def run(config: Optional[dict] = None):
    global _pool
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    log.info("=== CEX NPU Watchdog ===")
    log.info("State file: %s", STATE_FILE)

    _pool = NpuServerPool()
    _pool.load_from_config()

    disc_cfg = _pool._cfg.get("discovery", {})
    scan_interval = int(disc_cfg.get("scan_interval_s", 30))

    # Initial scan
    log.info("Initial LAN discovery...")
    _pool.discover_lan()
    _pool.probe_all()
    _pool.write_state()

    # Start HTTP API
    threading.Thread(target=_run_api, daemon=True).start()

    # Background loop: probe + re-scan
    while True:
        time.sleep(scan_interval)
        _pool.probe_all()
        _pool.discover_lan()
        _pool.write_state()
        available = _pool.get_available()
        log.info("Pool status: %d/%d NPU servers available",
                 len(available), len(_pool._servers))


if __name__ == "__main__":
    run()
