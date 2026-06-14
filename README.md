# Winx64 NPU using ARM on local LAN

Windows 10/11 x64 driver that routes NPU inference to one or more
Qualcomm ARM Linux servers on the local LAN.

**No limit on the number of ARM NPU servers.** Each server is discovered
automatically and added to the active pool. The orchestrator sees the
aggregate compute capacity.

## What this installs

| Component | Description |
|-----------|-------------|
| `cex_virt_npu.sys` | WDDM kernel driver -- shows NPU tab in Task Manager (Win11 23H2+) |
| `cex_npu_ep.dll` | ONNX Runtime custom EP -- `CexNpuExecutionProvider` |
| `cex_npu_proxy.py` | Windows service -- round-robin routes to N ARM servers |
| `npu_watchdog.py` | Windows service -- discovers + monitors all NPU servers on LAN |
| `config/npu_connections.json` | Server list + discovery settings (editable) |

## Architecture

```
[Windows 10/11 x64]
  Task Manager NPU tab (Win11 only)
        |
  cex_virt_npu.sys   WDDM, D3DKMT_NODETYPE_ML
        |  shared memory "CexNpuUtil"
  npu_watchdog.py    discovers all NPU servers, port 7475 HTTP API
        |
  cex_npu_proxy.py   round-robin across N servers, named pipe \\.\pipe\CexNpuProxy
        |
  cex_npu_ep.dll     CexNpuExecutionProvider for ONNX Runtime
        |
        | TCP port 7474 (CXNP binary frames)
        |
[ARM server 1]  [ARM server 2]  ...  [ARM server N]
 port 7474       port 7474              port 7474
 port 7476       port 7476              port 7476
 (health)        (health)               (health)
```

## Adding NPU Servers (no limit)

**Static (always available):** Edit `config/npu_connections.json`:
```json
{"servers": [
  {"id": "npu-01", "host": "192.168.1.100", "enabled": true, "label": "Room A"},
  {"id": "npu-02", "host": "192.168.1.101", "enabled": true, "label": "Room B"},
  {"id": "npu-03", "host": "192.168.1.200", "enabled": true, "label": "Office"}
]}
```

**Auto-discovery:** The watchdog scans the configured subnet every 30 seconds.
Any machine responding on port 7476 is automatically added to the pool.

## Ports

See full documentation: [docs/ports_and_stack.md](docs/ports_and_stack.md)

| Port | Role | Protocol |
|------|------|----------|
| 7474 | NPU inference (ARM server RX) | TCP/CXNP |
| 7475 | Watchdog API (TX, read-only) | HTTP/JSON |
| 7476 | Health check (ARM server) | HTTP/JSON |
| 7477 | Signal bus | SSE/WebSocket |

## Windows 10 vs Windows 11

| Feature | Windows 10 | Windows 11 (23H2+) |
|---------|-----------|---------------------|
| NPU tab in Task Manager | No | Yes |
| Inference routing | Yes | Yes |
| ONNX Runtime EP | Yes | Yes |
| Named pipe proxy | Yes | Yes |
| Auto-discovery | Yes | Yes |

On Windows 10 the driver installs and routes inference correctly.
The NPU utilization tab only appears on Windows 11 Build 22631+.

## Quick Start (ARM server side)

On the Qualcomm ARM Linux machine:
```bash
git clone https://github.com/compuword/cex-npu-linux
cd cex-npu-linux/server
sudo ./install_server.sh
curl http://localhost:7476/health
```

## Quick Start (Windows client side)

```powershell
# 1. Edit config (add your ARM server IPs)
notepad config\npu_connections.json

# 2. Test connectivity first
python service\npu_watchdog.py   # runs in foreground, shows discovered servers

# 3. Full install (Admin, after building driver + EP DLL)
Set-ExecutionPolicy Bypass -Scope Process
.\install\install_client.ps1 -ServerHost 192.168.1.100
```

## Check total compute

```powershell
# Query watchdog API (after watchdog is running)
Invoke-RestMethod http://localhost:7475/compute | ConvertTo-Json
```

```json
{
  "npu_server_count": 3,
  "total_npu_servers": 3,
  "servers_online": ["192.168.1.100", "192.168.1.101", "192.168.1.200"]
}
```

## Related

- [cex-npu-linux](https://github.com/compuword/cex-npu-linux) -- ARM server
- [cex-orchestrator](https://github.com/compuword/cex-orchestrator) -- GPU + NPU routing
- [cex-resource-discovery](https://github.com/compuword/cex-resource-discovery) -- unified LAN scanner
