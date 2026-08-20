# vios_res_client.sh — Client LPAR Resilience Test Helper

Run this **on the Client LPAR** (not the VIOS). It combines:
- **Layer 1 (auto-discovery):** finds `enX` interfaces, `hdiskX` disks, and the ping target (default gateway) automatically — no manual device names.
- **Layer 2 (evidence logging):** captures timestamped text logs of network/disk state and a continuous ping + disk-I/O monitor during failover — no screenshots.
- **Multi-network monitoring:** pings a target IP on EACH network interface (not just the default gateway) to verify all paths survive failover.
- **Restart detection:** auto-detects when a rebooted VIOS comes back up via ping, then captures the after-snapshot automatically.
- **Adapter location codes / virtual slot map:** runs `lscfg` on every `fcsX` (FC/NPIV) and `entX` (virtual ethernet) adapter and decodes which **virtual adapter slot** of the LPAR each one uses — so you can tell which VIOS serves which adapter.

All output goes to a **per-host, per-run** folder: `/tmp/vios_res_<hostname>_<timestamp>/`.
At the end, everything is bundled into a single tar: `/tmp/vios_res_<hostname>_<timestamp>.tar.gz`.

## Commands

| Command | What it does |
|---------|--------------|
| `./vios_res_client.sh discover` | Auto-detects devices, writes `inventory_<ts>.txt` + `adapter_slots_<ts>.txt` |
| `./vios_res_client.sh adapters` | `lscfg` location codes for all `fcsX`/`entX` + virtual slot map |
| `./vios_res_client.sh snapshot <label>` | One-shot capture of disk paths + network state |
| `./vios_res_client.sh monitor 180` | Continuous ping + disk read for 180s (run while VIOS is down) |
| `./vios_res_client.sh test vios1 shutdown 180` | Shutdown test: discover → before → 180s monitor → after_shutdown → tar |
| `./vios_res_client.sh test vios1 restart 180` | Restart test: discover → before → 180s monitor → wait for VIOS up → after_restart → tar |
| `./vios_res_client.sh recover` | Re-enable any `Failed` MPIO paths with `chpath` |
| `./vios_res_client.sh package` | Manually tar the results folder |
| `./vios_res_client.sh netsetup` | Interactive setup of per-interface ping targets |

## Test Modes

### Shutdown mode (`test <label> shutdown <seconds>`)

```
./vios_res_client.sh test vios1 shutdown 180
```

1. Discovers devices
2. Takes **before** snapshot: `snapshot_vios1_before_shutdown_<ts>.txt`
3. Monitors for 180 seconds (you shut down the VIOS during this window)
4. Takes **after** snapshot: `snapshot_vios1_after_shutdown_<ts>.txt`
5. Packages results

### Restart mode (`test <label> restart [seconds]`)

```
./vios_res_client.sh test vios1 restart 180
```

1. Discovers devices
2. Takes **before** snapshot: `snapshot_vios1_before_restart_<ts>.txt`
3. Starts background monitoring (gateway ping, disk I/O, multi-net)
4. **Foreground watches VIOS IP** — pings every 2s waiting for it to go down
5. As soon as VIOS is confirmed DOWN (3 consecutive ping failures), **immediately** captures **during** snapshot: `snapshot_vios1_during_restart_<ts>.txt` (this captures Failed paths while VIOS is truly down)
6. Waits for VIOS to respond to ping again (timeout: 600s)
7. Waits 30s for VIOS services to stabilize
8. Takes **after** snapshot: `snapshot_vios1_after_restart_<ts>.txt`
9. Stops background monitors, summarizes, packages results

The key improvement: the **during** snapshot is captured when VIOS goes down, after waiting for AIX to actually mark paths as `Failed`. The timing is:
- VIOS down detected (3 ping failures) → wait 30s → poll `lspath` every 5s for up to 30 more seconds until a `Failed` path appears → then capture snapshot.

This ensures you see the actual `Failed` paths before `hcheck_interval` auto-recovers them. (AIX typically takes 20-60s to transition paths from `Enabled` to `Failed` after the underlying VIOS transport is lost.)

**How restart detection gets the VIOS IP:**
- If `-V <ip>` is specified, uses that IP directly
- Otherwise, tries to resolve the label (e.g. `vios1`) via DNS/hosts
- If DNS fails, prompts the user interactively

```
# Explicit VIOS IP:
./vios_res_client.sh -V 10.0.0.5 test vios1 restart 180

# Let script resolve 'vios1' or ask you:
./vios_res_client.sh test vios1 restart 180
```

Default restart timeout (how long to wait for VIOS to come back): **600 seconds** (10 minutes).

## Adapter Location Codes & Virtual Adapter Slots

To know **which virtual adapter slot of the LPAR** an adapter occupies (and therefore which VIOS
serves it), the script runs `lscfg` for every `fcsX` and `entX` adapter and decodes the
location code:

```
./vios_res_client.sh adapters
```

### Decoding the location code

```
U8284.22A.21ABCDE-V3-C12-T1
|                  |  |   |
|                  |  |   +-- T1  = port number on the adapter
|                  |  +------ C12 = the LPAR's VIRTUAL ADAPTER SLOT   <-- the key value
|                  +--------- V3  = LPAR (partition) id
+---------------------------- U8284.22A.21ABCDE = machine type/model/serial
```

- A location code containing `-V<n>-` is a **virtual** adapter (served by a VIOS).
- A location code containing `-P<n>-` is a **physical** adapter (real slot on a planar).

### Example output

```
DEVICE   LOCATION CODE                    TYPE     LPAR-ID SLOT   PORT DESCRIPTION
-------- -------------------------------- -------- ------- ------ ---- -----------
fcs0     U8284.22A.21ABCDE-V3-C12-T1      VIRTUAL  3       12     1    Virtual Fibre Channel Client Adapter
fcs1     U8284.22A.21ABCDE-V3-C22-T1      VIRTUAL  3       22     1    Virtual Fibre Channel Client Adapter
ent0     U8284.22A.21ABCDE-V3-C2-T1       VIRTUAL  3       2      1    Virtual I/O Ethernet Adapter (l-lan)
ent1     U8284.22A.21ABCDE-V3-C3-T1       VIRTUAL  3       3      1    Virtual I/O Ethernet Adapter (l-lan)

-- Virtual adapter slots in use by this LPAR --
  fcs0 -> LPAR id 3, virtual slot C12  (match slot C12 on the HMC to find the serving VIOS)
  fcs1 -> LPAR id 3, virtual slot C22
  ent0 -> LPAR id 3, virtual slot C2
  ent1 -> LPAR id 3, virtual slot C3

-- Slot grouping (adapters sharing a slot come from the same VIOS) --
  slot 2     : ent0
  slot 3     : ent1
  slot 12    : fcs0
  slot 22    : fcs1
```

### How to use it

1. Note the `C<slot>` value for each adapter.
2. On the HMC, look at the client LPAR's virtual adapter profile: each client slot `C<n>` maps to
   a **server slot on a specific VIOS** (`vfchost*` for FC/NPIV, SEA/`vent*` for ethernet).
3. Adapters in **different slots served by different VIOS** = your dual-VIOS redundancy.
   `fcs0` via VIOS1 and `fcs1` via VIOS2 means shutting down VIOS1 should only fail `fcs0`'s paths.

This makes it easy to predict — and then verify in the snapshots — exactly which `hdisk` paths and
which `enX` interfaces should be affected when a given VIOS is shut down.

Where it appears:
- `adapter_slots_<ts>.txt` — full table + raw `lscfg -vpl` VPD output + FC WWPNs
- `inventory_<ts>.txt` — summary tables under "FC adapters" / "Ethernet adapters"
- Every `snapshot_*.txt` — table at the top plus `lscfg -vl` per adapter, so a
  before/during/after diff shows if an adapter or its slot changed state

## Options
- `-t <ip>` — set ping target manually (otherwise auto-detects the default gateway)
- `-V <ip>` — set VIOS IP for restart detection (otherwise resolves from label or prompts)
- `-h` — help

## Multi-Network Interface Ping

LPARs often have multiple network interfaces (en0, en1, en2, ...) routed through different VIOSes or VLANs. To verify ALL networks survive a failover, the script can ping a specific target IP for each interface.

### Setup

Run the interactive setup:
```
./vios_res_client.sh netsetup
```

This will:
1. Discover all `en*` interfaces and show their current IPs
2. Ask you for a target IP for each interface (e.g. a gateway or host on that subnet)
3. Save the config to `./vios_res_net_targets.conf` (same directory as the script)

### Config file format (`./vios_res_net_targets.conf`)

```
# vios_res_client network targets configuration
# Format: interface=target_ip
en0=10.1.1.1
en1=10.2.2.1
en2=192.168.5.1
```

You can also create/edit this file manually.

### How it works

- **If the config file exists:** During `monitor` and `snapshot`, the script pings each configured target in addition to the default gateway. Results are logged in `monitor_multinet_<ts>.txt`.
- **If no config file:** Only the default gateway is pinged (original behavior). The `discover` command will remind you to run `netsetup`.

### Multi-net monitor output

The `monitor_multinet_<ts>.txt` log shows per-interface results:
```
09:15:01 en0 OK   10.1.1.1 64 bytes from 10.1.1.1: time=0.5ms
09:15:01 en1 OK   10.2.2.1 64 bytes from 10.2.2.1: time=0.8ms
09:15:03 en0 LOSS 10.1.1.1 no reply
09:15:03 en1 OK   10.2.2.1 64 bytes from 10.2.2.1: time=0.7ms
```

This lets you pinpoint exactly which network was affected and for how long.

## Typical Test Workflows

### Workflow 1: VIOS Shutdown Test (VIOS stays down)

```bash
# 1. Setup multi-network targets (first time only)
./vios_res_client.sh netsetup

# 2. Run the full shutdown test
./vios_res_client.sh test vios1 shutdown 180
#    -> When you see ">>> SHUT DOWN THE TARGET VIOS NOW <<<", shut down VIOS1 from the HMC.
#    -> Wait 180s for monitoring to complete.
#    -> After-snapshot captured automatically.
```

### Workflow 2: VIOS Restart Test (VIOS comes back up)

```bash
# 1. Setup multi-network targets (first time only)
./vios_res_client.sh netsetup

# 2. Run the full restart test
./vios_res_client.sh -V 10.0.0.5 test vios1 restart 180
#    -> When you see ">>> RESTART THE TARGET VIOS NOW <<<", reboot VIOS1 from the HMC.
#    -> 180s monitoring captures the disruption.
#    -> Script then pings VIOS IP every 5s until it responds.
#    -> After VIOS is up + 30s stabilization, after-snapshot is captured.
```

## Output Files (under `/tmp/vios_res_<hostname>_<ts>/`)

| File | Description |
|------|-------------|
| `inventory_<ts>.txt` | Discovered devices + lspath + net targets + adapter slot summary |
| `adapter_slots_<ts>.txt` | `lscfg` location codes for all `fcsX`/`entX`, virtual slot map, WWPNs |
| `snapshot_<label>_before_shutdown_<ts>.txt` | Before snapshot (shutdown mode) |
| `snapshot_<label>_after_shutdown_<ts>.txt` | After snapshot (shutdown mode) |
| `snapshot_<label>_before_restart_<ts>.txt` | Before snapshot (restart mode) |
| `snapshot_<label>_during_restart_<ts>.txt` | During snapshot (VIOS is down, restart mode) |
| `snapshot_<label>_after_restart_<ts>.txt` | After snapshot (VIOS is back up, restart mode) |
| `monitor_ping_<ts>.txt` | Per-second ping results (default gateway) |
| `monitor_vios_ping_<ts>.txt` | VIOS ping status log (restart mode: UP/DOWN transitions) |
| `monitor_multinet_<ts>.txt` | Per-interface ping results (multi-network) |
| `monitor_disk_<ts>.txt` | Per-second disk read results |
| `recover_<ts>.txt` | Output of chpath recovery actions |
| `vios_res_client.log` | Master run log |

Bundled: `/tmp/vios_res_<hostname>_<ts>.tar.gz`

## What to Look For (Pass/Fail)

- **Network OK:** `monitor_ping` and `monitor_multinet` have 0 or very few `LOSS` lines during shutdown/restart.
- **Disk OK:** `monitor_disk` has no `ERROR`; a brief `SLOW` during failover may be acceptable.
- **Paths:** `snapshot ..._after_*` shows the failed VIOS's paths as `Failed`, the rest `Enabled`; after recovery all return `Enabled`.
- **Restart:** In restart mode, all paths should return to `Enabled` in the after-snapshot (if `hcheck_interval > 0`).
- **Adapter slots:** `adapter_slots_<ts>.txt` should show FC adapters (and ethernet adapters) in
  **two different virtual slots** — one per VIOS. If all adapters share a single slot, the LPAR is
  served by only one VIOS and has **no redundancy** (that is a config finding, not a test failure).

## Will Failed Paths Auto-recover After the VIOS Boots? (important)

This depends on the disk attribute **`hcheck_interval`** (the snapshot captures it for each hdisk):

- **`hcheck_interval > 0`** (e.g. 60) with `hcheck_mode=nonactive` → AIX polls the paths and
  **automatically re-enables** them once the VIOS is back. **No `chpath` needed.**
- **`hcheck_interval = 0`** (health check disabled) → the path **stays `Failed`** even after the
  VIOS returns. You must manually enable it — use:
  ```
  ./vios_res_client.sh recover
  ```
  which runs `chpath -l hdiskX -p <parent> -s enable` for every Failed path automatically.

Tip: after the test, if `lspath` still shows `Failed` paths a few minutes after the VIOS is up,
run `recover`. Best practice is to set `hcheck_interval` to a non-zero value (e.g. 60) so recovery
is automatic.

## Generating the Word Report

After the test completes and results are in the `testresult/` directory, use the Python script to generate a consolidated Word document (.docx):

### Prerequisites

```bash
# Install python-docx (one time only)
pip install python-docx
```

### Usage

```bash
# Run from the project root directory
python scripts/generate_vios_report.py
```

The script will:
1. Scan `testresult/` for all `vios_res_*` directories
2. Parse the inventory, monitor logs, and snapshot files
3. Generate a `.docx` report for each test run at:
   `testresult/VIOS_Resilience_Report_vios_res_<hostname>_<timestamp>.docx`

### Report Structure

The generated Word document contains:

| Section | Content |
|---------|---------|
| Executive Summary | Pass/Fail result, VIOS downtime, key metrics |
| Test Environment | Client inventory (interfaces, disks, MPIO paths) |
| VIOS Status Timeline | VIOS UP/DOWN/Recovered transitions |
| Before VIOS Shutdown | Network + Disk path state (all paths Enabled) |
| During VIOS Shutdown | Impact on network ping + disk I/O, failed paths |
| After VIOS Resume | Recovery confirmation (paths back to Enabled) |
| Conclusion | Comparison table + overall assessment |

Each of the Before/During/After sections is split into:
- **Network Path Resilience** — ping results, interface stats
- **Disk/SAN Path Resilience** — disk I/O results, MPIO path status, errpt

### Example

```bash
# After running vios_res_client.sh test on the AIX LPAR and copying results to testresult/:
(base) $ python scripts/generate_vios_report.py
Found 1 test result directory(ies):
  - vios_res_aix14114_20260806_115317

Processing: vios_res_aix14114_20260806_115317
Report generated: testresult/VIOS_Resilience_Report_vios_res_aix14114_20260806_115317.docx
```

Then open the `.docx` file in Microsoft Word or any compatible editor.

## Notes
- Written in ksh, same conventions as `lsseasV4.sh`.
- Disk test uses a small read-only `dd` from the first discovered hdisk (safe, non-destructive).
- Run as root for complete `errpt` output.
- The VIOS shutdown/restart itself stays manual (HMC) by design — the script automates everything around it.
- Version 1.2: Added restart mode, multi-network ping, improved snapshot naming.
- Version 1.3: Added `generate_vios_report.py` for Word document report generation.
- Version 1.3 (script): Added `adapters` command — `lscfg` location codes for `fcsX` and `entX`
  adapters with virtual adapter slot decoding (`-V<lparid>` / `-C<slot>` / `-T<port>`). The slot
  map is now also included in `discover` and in every snapshot.
