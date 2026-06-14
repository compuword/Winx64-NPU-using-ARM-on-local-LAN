# Winx64 NPU using ARM on local LAN -- Ports & Connection Stack

## Port Assignment

All ports are TCP, above 1024 (per CEX network policy).

| Port | Role | Direction | Auth | Protocol | Description |
|------|------|-----------|------|----------|-------------|
| **7474** | NPU RX | Client -> ARM server | X-CEX-Key | TCP/CXNP | ONNX inference requests |
| **7475** | TX / Watchdog API | Outbound / clients <- | none (LAN only) | HTTP/JSON | Watchdog status, resource inventory |
| **7476** | Health | Both directions | none | HTTP/JSON | ARM server `/health` endpoint |
| **7477** | Signal bus | Both directions | X-CEX-Key | SSE/WebSocket | Real-time event stream |
| 7478+ | Expansion | Configurable | Configurable | CXNP or HTTP | Additional servers/GPU resources |

## Connection Stack (per NPU server)

```
[Windows 10/11 client]
  +--[WDDM kernel driver: cex_virt_npu.sys]
  |    D3DKMT_NODETYPE_ML -> NPU tab in Task Manager
  |    reads shared memory "CexNpuUtil" (float32 utilization)
  |
  +--[npu_watchdog.py  -- Windows service, port 7475]
  |    - Loads config/npu_connections.json (N servers, no limit)
  |    - Auto-discovers servers via LAN scan (subnet in config)
  |    - Probes health port 7476 every 15s
  |    - Writes C:\ProgramData\CexNPU\state.json
  |    - Exposes HTTP API: GET /resources  GET /compute
  |
  +--[cex_npu_proxy.py -- Windows service, named pipe \\.\pipe\CexNpuProxy]
  |    - Named pipe server for ORT custom EP (cex_npu_ep.dll)
  |    - TCP client -> selects best available NPU server (round-robin)
  |    - Writes float32 util% to shared memory "CexNpuUtil"
  |
  +--[cex_npu_ep.dll  -- ONNX Runtime custom EP, user mode]
       providers=['CexNpuExecutionProvider']
       app-transparent routing via named pipe

                        | TCP port 7474 (CXNP frames)
                        | X-CEX-Key header
                        v

[ARM Linux server #1]  [ARM Linux server #2]  [ARM Linux server N]
  cex_npu_server.py     cex_npu_server.py      cex_npu_server.py
  QNNExecutionProvider  QNNExecutionProvider   (any provider)
  port 7474             port 7474              port 7474
  health: 7476          health: 7476           health: 7476
```

## Multi-NPU Support (no limit)

The watchdog manages a dynamic pool of NPU servers. New servers are added by:

1. **Static config** -- edit `config/npu_connections.json`:
   ```json
   {"servers": [
     {"id": "npu-01", "host": "192.168.1.100", "enabled": true},
     {"id": "npu-02", "host": "192.168.1.101", "enabled": true},
     {"id": "npu-03", "host": "192.168.1.102", "enabled": true}
   ]}
   ```

2. **Auto-discovery** -- watchdog scans the configured subnet every 30s.
   Any machine responding on port 7476 is automatically added to the pool.

3. **Runtime API** -- POST to watchdog API (future endpoint):
   ```
   POST http://localhost:7475/servers  {"host": "192.168.1.150"}
   ```

## Total Compute Visibility

The orchestrator queries `GET http://localhost:7475/compute`:

```json
{
  "npu_server_count": 3,
  "total_npu_servers": 5,
  "active_providers": ["QNNExecutionProvider"],
  "servers_online": ["192.168.1.100", "192.168.1.101", "192.168.1.102"]
}
```

Full state including per-server capabilities: `GET http://localhost:7475/resources`

State is also written to `C:\ProgramData\CexNPU\state.json` for offline access.

## CXNP Protocol Frame Format

```
+--4 bytes--+--4 bytes--+--4 bytes--+--N bytes--+
|   CXNP   | msg_type  | payload   |  payload  |
|  (magic)  |  (uint32) | len(u32)  |  bytes    |
+-----------+-----------+-----------+-----------+
```

Message types: INFER_REQUEST=0x10, INFER_RESPONSE=0x11,
               HEARTBEAT=0x20, HEARTBEAT_ACK=0x21, ERROR=0xFF

Full spec: `shared/cex_npu_protocol.py`

## Security

- All TCP traffic stays on LAN (`CEX_NET_KEY` env var for auth)
- Watchdog API (7475) is read-only -- no write endpoints yet
- WDDM driver uses Windows ACLs for shared memory access
- Health endpoint (7476) has no auth -- returns only stats

## Windows 10 Compatibility

- Windows 10 Build 19041+ (2004) -- WDDM 2.7+
- NPU tab in Task Manager: Windows 11 Build 22631+ (23H2) only
  - On Windows 10: driver installs and routes inference correctly
    but the NPU tab does NOT appear (Task Manager limitation)
  - GPU/CPU performance tabs still show on Windows 10
- Named pipe API: Windows 10 / 11 both fully supported
- DirectML (GPU path): Windows 10 Build 1709+ (Fall Creators Update)
