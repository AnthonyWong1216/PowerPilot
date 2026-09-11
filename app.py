"""
PowerPilot - HMC & Power Server Management Web Application
Flask backend with SSH and HMC REST API support
"""

import os
import re
import json
import glob
import logging
import subprocess
import shlex

from flask import (Flask, render_template, request, jsonify, session,
                   send_from_directory, send_file)
from flask_socketio import SocketIO, emit


from modules.ssh_manager import SSHManager
from modules.hmc_api import HMCApiClient
from modules.hmc_store import HMCStore
from modules.san_store import SANStore
from modules.san_manager import SANManager
from modules.storage_store import StorageStore
from modules.nim_store import NIMStore



logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "powerpilot-dev-secret-change-in-prod")
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

ssh_manager = SSHManager()
hmc_store = HMCStore()
san_store = SANStore()
san_manager = SANManager(ssh_manager)
storage_store = StorageStore()
nim_store = NIMStore()



# ──────────────────────────────────────────────────────────
# Page routes
# ──────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/connect")
def connect_page():
    return render_template("connect.html")


@app.route("/lpars")
def lpars_page():
    return render_template("lpars.html")


@app.route("/console/<hmc_id>/<system_id>/<lpar_name>")
def console_page(hmc_id, system_id, lpar_name):
    """Standalone popup page for the LPAR vterm console."""
    hmc = hmc_store.get(hmc_id)
    if not hmc:
        return "HMC not found", 404
    return render_template(
        "console.html",
        hmc_id=hmc_id,
        system_id=system_id,
        lpar_name=lpar_name,
    )

@app.route("/topology")
def topology_page():
    return render_template("topology.html")


@app.route("/create_lpar", methods=["GET", "POST"])
def create_lpar():
    """Render the Create LPAR form (GET) or execute mksyscfg via SSH (POST)."""
    if request.method == "GET":
        return render_template("create_lpar.html")

    f = request.form

    # ── Identify the HMC and managed system ──────────────────
    hmc_id    = f.get("hmc_id")       # passed as hidden field (see below)
    system_id = f.get("managed_systems")
    hmc = hmc_store.get(hmc_id) if hmc_id else None

    # Fall back to any connected HMC when hmc_id not supplied via form
    if not hmc:
        all_hmcs = hmc_store.list()
        hmc = all_hmcs[0] if all_hmcs else None

    if not hmc:
        return jsonify({"error": "No HMC selected or configured"}), 400
    if not system_id:
        return jsonify({"error": "No managed system selected"}), 400

    # ── Build virtual_eth_adapters list ──────────────────────
    # mksyscfg format: slot/ieee/pvid/addl_vlans/is_trunk/is_required/vswitch///
    veth_parts = []
    slots      = f.getlist("veth_slot[]")
    pvids      = f.getlist("veth_pvid[]")
    vswitches  = f.getlist("veth_vswitch[]")
    addl_vlans = f.getlist("veth_addl_vlans[]")

    for i, slot in enumerate(slots):
        if not slot:
            continue
        pvid    = pvids[i]      if i < len(pvids)      else ""
        vswitch = vswitches[i]  if i < len(vswitches)  else ""
        addl    = addl_vlans[i] if i < len(addl_vlans) else ""
        veth_parts.append(f"{slot}/0/{pvid}/{addl}/0/0/{vswitch}///")

    virtual_eth_str = veth_parts   # list — ssh_manager wraps each spec individually

    # ── Build virtual_fc_adapters list ───────────────────────
    # Each spec: slot/client/vios_lpar_id/vios_lpar_name/vios_slot/wwpn1,wwpn2/is_required
    # Passed as a list so ssh_manager wraps each spec in ""..."" without
    # accidentally splitting on the comma inside the wwpn1,wwpn2 pair.
    vfc_parts     = []
    vfc_slots     = f.getlist("vfc_slot[]")
    vfc_vios      = f.getlist("vfc_vios[]")
    vfc_vios_slot = f.getlist("vfc_vios_slot[]")
    vfc_wwpn1     = f.getlist("vfc_wwpn1[]")
    vfc_wwpn2     = f.getlist("vfc_wwpn2[]")
    vfc_req_ded   = f.getlist("vfc_require_dedicated[]")

    for i, slot in enumerate(vfc_slots):
        if not slot:
            continue
        vios_name = vfc_vios[i]      if i < len(vfc_vios)      else ""
        vios_slot = vfc_vios_slot[i] if i < len(vfc_vios_slot) else ""
        wwpn1_raw = (vfc_wwpn1[i]    if i < len(vfc_wwpn1)     else "").replace(":", "").lower()
        wwpn2_raw = (vfc_wwpn2[i]    if i < len(vfc_wwpn2)     else "").replace(":", "").lower()
        req_ded   = vfc_req_ded[i]   if i < len(vfc_req_ded)   else "0"
        vios_id   = _resolve_vios_id(hmc, system_id, vios_name)
        # Format: slot/client/vios_id/vios_name/vios_slot/wwpn1,wwpn2/is_required
        vfc_parts.append(
            f"{slot}/client/{vios_id}/{vios_name}/{vios_slot}/{wwpn1_raw},{wwpn2_raw}/{req_ded}"
        )

    # Keep as list — ssh_manager wraps each entry in ""..."" individually
    virtual_fc_str = vfc_parts

    # ── Build virtual_scsi_adapters string ───────────────────
    # mksyscfg format: slot/client/vios_lpar_id/vios_lpar_name/vios_slot/0
    vscsi_parts     = []
    vscsi_slots     = f.getlist("vscsi_slot[]")
    vscsi_vios      = f.getlist("vscsi_vios[]")
    vscsi_vios_slot = f.getlist("vscsi_vios_slot[]")

    for i, slot in enumerate(vscsi_slots):
        if not slot:
            continue
        vios_name = vscsi_vios[i]      if i < len(vscsi_vios)      else ""
        vios_slot = vscsi_vios_slot[i] if i < len(vscsi_vios_slot) else ""
        vios_id   = _resolve_vios_id(hmc, system_id, vios_name)
        vscsi_parts.append(f"{slot}/client/{vios_id}/{vios_name}/{vios_slot}/0")

    virtual_scsi_str = vscsi_parts   # list — ssh_manager wraps each spec individually

    # ── Assemble mksyscfg parameter dict ─────────────────────
    params = {
        "name":                       f.get("name"),
        "lpar_id":                    f.get("lpar_id"),
        "lpar_env":                   f.get("lpar_env", "aixlinux"),
        "profile_name":               f.get("profile_name", "default_profile"),
        "boot_mode":                  f.get("boot_mode", "norm"),
        "lpar_proc_compat_mode":      f.get("lpar_proc_compat_mode", "default"),
        "max_virtual_slots":          f.get("max_virtual_slots", "100"),
        "conn_monitoring":            "1" if f.get("conn_monitoring") else "0",
        "sync_curr_profile":          "1" if f.get("sync_curr_profile") else "0",
        "allow_perf_collection":      "1" if f.get("shared_proc_pool_util_auth") else "0",
        "proc_mode":                  f.get("proc_mode", "shared"),
        "sharing_mode":               f.get("sharing_mode", "uncap"),
        "min_proc_units":             f.get("min_proc_units"),
        "desired_proc_units":         f.get("desired_proc_units"),
        "max_proc_units":             f.get("max_proc_units"),
        "min_procs":                  f.get("min_procs"),
        "desired_procs":              f.get("desired_procs"),
        "max_procs":                  f.get("max_procs"),
        "uncap_weight":               f.get("uncap_weight"),
        "min_mem":                    f.get("min_mem"),
        "desired_mem":                f.get("desired_mem"),
        "max_mem":                    f.get("max_mem"),
        "virtual_eth_adapters":       virtual_eth_str,
        "virtual_fc_adapters":        virtual_fc_str,
        "virtual_scsi_adapters":      virtual_scsi_str,
    }

    result = ssh_manager.create_lpar(
        host=hmc["host"],
        port=int(hmc.get("ssh_port", 22)),
        username=hmc.get("username", "hscroot"),
        key_path=hmc.get("key_path") or None,
        managed_system=system_id,
        params=params,
    )

    if result.get("ok"):
        return jsonify({
            "ok":      True,
            "message": result.get("message", "LPAR created successfully."),
            "command": result.get("command", ""),
        }), 201
    return jsonify({
        "ok":      False,
        "error":   result.get("error", "mksyscfg failed"),
        "stderr":  result.get("stderr", ""),
        "command": result.get("command", ""),
    }), 400


def _resolve_vios_id(hmc: dict, system_id: str, vios_name: str) -> str:
    """Look up the numeric lpar_id of a named VIOS via lssyscfg. Returns '0' on failure."""
    if not vios_name:
        return "0"
    result = ssh_manager.run_hmc_command(
        host=hmc["host"],
        port=int(hmc.get("ssh_port", 22)),
        username=hmc.get("username", "hscroot"),
        key_path=hmc.get("key_path") or None,
        command=(f'lssyscfg -r lpar -m "{system_id}" '
                 f'--filter "lpar_names={vios_name}" -F lpar_id'),
    )
    if result.get("ok"):
        return result["output"].strip().splitlines()[0].strip() or "0"
    return "0"

@app.route("/managed-systems")
def managed_systems_page():
    return render_template("managed_systems.html")


@app.route("/virtual-networks")
def virtual_networks_page():
    return render_template("virtual_networks.html")


@app.route("/storage")
def storage_page():
    return render_template("storage.html")


@app.route("/jobs")
def jobs_page():
    return render_template("jobs.html")


@app.route("/deploy-scripts")
def deploy_scripts_page():
    return render_template("deploy_scripts.html")


@app.route("/test")
def test_page():
    return render_template("test.html")




@app.route("/san-switches")
def san_switches_page():
    return render_template("san_switches.html")


@app.route("/zoning")
def zoning_page():
    return render_template("zoning.html")


@app.route("/vfc-map")
def vfc_map_page():
    return render_template("vfc_map.html")


@app.route("/nim")
def nim_page():
    return render_template("nim.html")



# ──────────────────────────────────────────────────────────
# HMC connection management API
# ──────────────────────────────────────────────────────────

@app.route("/api/hmcs", methods=["GET"])
def list_hmcs():
    """Return all saved HMC connections."""
    return jsonify(hmc_store.list())


@app.route("/api/hmcs", methods=["POST"])
def add_hmc():
    """Save a new HMC entry (no connection attempt yet)."""
    data = request.get_json(force=True)
    required = ["name", "host"]
    missing = [f for f in required if not data.get(f)]
    if missing:
        return jsonify({"error": f"Missing fields: {', '.join(missing)}"}), 400
    entry = hmc_store.add(data)
    return jsonify(entry), 201


@app.route("/api/hmcs/<hmc_id>", methods=["DELETE"])
def delete_hmc(hmc_id):
    hmc_store.remove(hmc_id)
    return jsonify({"ok": True})


@app.route("/api/hmcs/<hmc_id>/test-ssh", methods=["POST"])
def test_ssh(hmc_id):
    """Test SSH connectivity to an HMC."""
    hmc = hmc_store.get(hmc_id)
    if not hmc:
        return jsonify({"error": "HMC not found"}), 404
    data = request.get_json(force=True) or {}
    result = ssh_manager.test_connection(
        host=hmc["host"],
        port=int(hmc.get("ssh_port", 22)),
        username=data.get("username", hmc.get("username", "hscroot")),
        key_path=data.get("key_path") or hmc.get("key_path"),
        password=data.get("password"),
    )
    return jsonify(result)


@app.route("/api/hmcs/<hmc_id>/push-key", methods=["POST"])
def push_ssh_key(hmc_id):
    """Copy an SSH public key to the HMC (ssh-copy-id equivalent)."""
    hmc = hmc_store.get(hmc_id)
    if not hmc:
        return jsonify({"error": "HMC not found"}), 404
    data = request.get_json(force=True) or {}
    result = ssh_manager.push_public_key(
        host=hmc["host"],
        port=int(hmc.get("ssh_port", 22)),
        username=data.get("username", hmc.get("username", "hscroot")),
        password=data.get("password", ""),
        public_key_path=data.get("public_key_path", "~/.ssh/id_rsa.pub"),
    )
    return jsonify(result)


@app.route("/api/ssh-keys", methods=["GET"])
def list_ssh_keys():
    """Return SSH keys found in ~/.ssh/."""
    return jsonify(ssh_manager.list_local_keys())


@app.route("/api/ssh-keys/generate", methods=["POST"])
def generate_ssh_key():
    """Generate a new RSA/ED25519 key pair."""
    data = request.get_json(force=True) or {}
    result = ssh_manager.generate_key(
        key_type=data.get("key_type", "ed25519"),
        key_name=data.get("key_name", "id_powerpilot"),
        passphrase=data.get("passphrase", ""),
        comment=data.get("comment", "powerpilot"),
    )
    return jsonify(result)


# ──────────────────────────────────────────────────────────
# Deploy Scripts API — list local scripts & push them to servers
# ──────────────────────────────────────────────────────────

SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts")


@app.route("/api/scripts", methods=["GET"])
def list_scripts():
    """Return the files found in the local scripts/ folder."""
    files = []
    try:
        for entry in sorted(os.listdir(SCRIPTS_DIR)):
            full = os.path.join(SCRIPTS_DIR, entry)
            if os.path.isfile(full):
                files.append({
                    "name": entry,
                    "size": os.path.getsize(full),
                })
    except FileNotFoundError:
        return jsonify({"ok": False, "error": "scripts folder not found"}), 404
    return jsonify({"ok": True, "data": files})


@app.route("/api/scripts/deploy", methods=["POST"])
def deploy_script():
    """Deploy a script from the local scripts/ folder to a remote server.

    Body: {filename, host, username, password, method (sftp|scp), target_path, port}
    """
    data = request.get_json(force=True) or {}
    filename    = (data.get("filename") or "").strip()
    host        = (data.get("host") or "").strip()
    username    = (data.get("username") or "").strip()
    password    = data.get("password") or ""
    method      = (data.get("method") or "sftp").strip().lower()
    target_path = (data.get("target_path") or "").strip()
    try:
        port = int(data.get("port") or 22)
    except (TypeError, ValueError):
        port = 22

    # Validate required fields
    missing = [k for k, v in [
        ("filename", filename), ("host", host), ("username", username),
    ] if not v]
    if missing:
        return jsonify({"ok": False, "error": f"Missing: {', '.join(missing)}"}), 400

    if method not in ("sftp", "scp"):
        method = "sftp"

    # Guard against path traversal — only allow files inside SCRIPTS_DIR.
    safe_name = os.path.basename(filename)
    local_path = os.path.join(SCRIPTS_DIR, safe_name)
    if not os.path.isfile(local_path):
        return jsonify({"ok": False, "error": f"Script not found: {safe_name}"}), 404

    result = ssh_manager.deploy_file(
        local_path=local_path,
        host=host,
        port=port,
        username=username,
        password=password,
        target_path=target_path,
        method=method,
    )
    return jsonify(result), (200 if result.get("ok") else 400)



# ──────────────────────────────────────────────────────────
# HMC REST API
# ──────────────────────────────────────────────────────────

@app.route("/api/hmcs/<hmc_id>/api-login", methods=["POST"])
def api_login(hmc_id):
    hmc = hmc_store.get(hmc_id)
    if not hmc:
        return jsonify({"error": "HMC not found"}), 404
    data = request.get_json(force=True) or {}
    client = HMCApiClient(
        host=hmc["host"],
        port=int(hmc.get("api_port", 443)),
        username=data.get("username", hmc.get("username", "hscroot")),
        password=data.get("password", hmc.get("api_password", "")),
    )
    result = client.login()
    if result.get("session_id"):
        hmc_store.update(hmc_id, {"session_id": result["session_id"],
                                   "api_client": True})
    return jsonify(result)


@app.route("/api/hmcs/<hmc_id>/api-logout", methods=["POST"])
def api_logout(hmc_id):
    hmc = hmc_store.get(hmc_id)
    if not hmc:
        return jsonify({"error": "HMC not found"}), 404
    if hmc.get("session_id"):
        client = HMCApiClient(
            host=hmc["host"],
            port=int(hmc.get("api_port", 443)),
            session_id=hmc["session_id"],
        )
        client.logout()
        hmc_store.update(hmc_id, {"session_id": "", "api_client": False})
    return jsonify({"ok": True})


@app.route("/api/hmcs/<hmc_id>/api-status", methods=["GET"])
def api_status(hmc_id):
    """Probe whether the stored session token is still alive."""
    hmc = hmc_store.get(hmc_id)
    if not hmc:
        return jsonify({"error": "HMC not found"}), 404
    sid = hmc.get("session_id", "")
    if not sid:
        return jsonify({"active": False, "reason": "no_session"})
    # Lightweight probe: fetch the top-level /rest/api/uom feed (tiny response)
    client = HMCApiClient(
        host=hmc["host"],
        port=int(hmc.get("api_port", 443)),
        session_id=sid,
    )
    try:
        r = client._get(f"{client.base}/uom", timeout=8)
        if r.status_code == 200:
            return jsonify({"active": True})
        if r.status_code == 401:
            hmc_store.update(hmc_id, {"session_id": "", "api_client": False})
            return jsonify({"active": False, "reason": "expired"})
        return jsonify({"active": False, "reason": f"HTTP {r.status_code}"})
    except Exception as exc:
        return jsonify({"active": False, "reason": str(exc)})


@app.route("/api/hmcs/<hmc_id>/managed-systems", methods=["GET"])
def get_managed_systems(hmc_id):
    hmc = hmc_store.get(hmc_id)
    if not hmc:
        return jsonify({"error": "HMC not found"}), 404

    method = request.args.get("method", "auto")  # auto | ssh | api

    # ── SSH fast-path ──────────────────────────────────────────────
    use_ssh = (method == "ssh") or (method == "auto" and not hmc.get("session_id"))
    if use_ssh:
        result = ssh_manager.run_hmc_command(
            host=hmc["host"],
            port=int(hmc.get("ssh_port", 22)),
            username=hmc.get("username", "hscroot"),
            key_path=hmc.get("key_path") or None,
            command="lssyscfg -r sys -F name:state:type_model",
        )
        if result.get("ok"):
            systems = []
            for line in result["output"].splitlines():
                line = line.strip()
                if not line:
                    continue
                parts = line.split(":")
                name  = parts[0] if len(parts) > 0 else "—"
                state = parts[1] if len(parts) > 1 else "—"
                model = parts[2] if len(parts) > 2 else "—"
                systems.append({"id": name, "name": name,
                                 "state": state, "model": model})
            return jsonify({"ok": True, "data": systems, "method": "ssh"})
        # SSH failed — fall through to API if session available
        if not hmc.get("session_id"):
            return jsonify({"ok": False,
                            "error": result.get("error", "SSH failed"),
                            "need_login": True, "method": "ssh_failed"})

    # ── REST API path ──────────────────────────────────────────────
    if not hmc.get("session_id"):
        return jsonify({"ok": False,
                        "error": "No API session — click 'API Login' on the Connect page first",
                        "need_login": True})
    client = HMCApiClient(
        host=hmc["host"],
        port=int(hmc.get("api_port", 443)),
        session_id=hmc.get("session_id"),
    )
    result = client.get_managed_systems()
    logger.debug("ManagedSystem raw XML:\n%s", result.get("raw", "")[:2000])
    result.pop("raw", None)
    result["method"] = "api"
    return jsonify(result)


@app.route("/api/hmcs/<hmc_id>/managed-systems/raw", methods=["GET"])
def get_managed_systems_raw(hmc_id):
    """Debug endpoint — returns the raw XML from the HMC."""
    hmc = hmc_store.get(hmc_id)
    if not hmc:
        return jsonify({"error": "HMC not found"}), 404
    if not hmc.get("session_id"):
        return jsonify({"error": "No API session"}), 401
    client = HMCApiClient(
        host=hmc["host"],
        port=int(hmc.get("api_port", 443)),
        session_id=hmc.get("session_id"),
    )
    result = client.get_managed_systems()
    return app.response_class(result.get("raw", ""), mimetype="text/xml")


@app.route("/api/hmcs/<hmc_id>/managed-systems/<system_id>/lpars", methods=["GET"])
def get_lpars(hmc_id, system_id):
    hmc = hmc_store.get(hmc_id)
    if not hmc:
        return jsonify({"error": "HMC not found"}), 404

    method = request.args.get("method", "auto")
    use_ssh = (method == "ssh") or (method == "auto" and not hmc.get("session_id"))

    if use_ssh:
        # lssyscfg -r lpar -m <system> -F name:lpar_id:state:lpar_env:rmc_ipaddr:os_version
        cmd = (f'lssyscfg -r lpar -m "{system_id}" '
               f'-F name:lpar_id:state:lpar_env:rmc_ipaddr:os_version')
        result = ssh_manager.run_hmc_command(
            host=hmc["host"], port=int(hmc.get("ssh_port", 22)),
            username=hmc.get("username", "hscroot"),
            key_path=hmc.get("key_path") or None,
            command=cmd,
        )
        if result.get("ok"):
            lpars = []
            for line in result["output"].splitlines():
                line = line.strip()
                if not line:
                    continue
                parts = line.split(":")
                lpars.append({
                    "name":       parts[0] if len(parts) > 0 else "—",
                    "lpar_id":    parts[1] if len(parts) > 1 else "—",
                    "id":         parts[0] if len(parts) > 0 else "—",
                    "state":      parts[2] if len(parts) > 2 else "—",
                    "type":       parts[3] if len(parts) > 3 else "—",
                    "ip_address": parts[4] if len(parts) > 4 else "—",
                    "os_version": parts[5] if len(parts) > 5 else "—",
                    "profile":    "—",
                })
            return jsonify({"ok": True, "data": lpars, "method": "ssh"})
        if not hmc.get("session_id"):
            return jsonify({"ok": False, "error": result.get("error", "SSH failed"),
                            "need_login": True})

    # REST API fallback
    client = HMCApiClient(
        host=hmc["host"],
        port=int(hmc.get("api_port", 443)),
        session_id=hmc.get("session_id"),
    )
    result = client.get_lpars(system_id)
    result.pop("raw", None)
    result["method"] = "api"
    return jsonify(result)


@app.route("/api/hmcs/<hmc_id>/managed-systems/<system_id>/lpars/<path:lpar_id>/detail",
           methods=["GET"])
def get_lpar_detail(hmc_id, system_id, lpar_id):
    """Return CPU, memory, virtual adapters for a single LPAR via SSH lssyscfg."""
    hmc = hmc_store.get(hmc_id)
    if not hmc:
        return jsonify({"error": "HMC not found"}), 404

    # Run ALL queries on a single SSH connection to avoid the HMC
    # dropping subsequent connections after the first exec_command.
    m = f'"{system_id}"'
    f = f'"lpar_names={lpar_id}"'
    raw = ssh_manager.run_hmc_commands_batch(
        host=hmc["host"], port=int(hmc.get("ssh_port", 22)),
        username=hmc.get("username", "hscroot"),
        key_path=hmc.get("key_path") or None,
        commands={
            "cpu": (
                f'lshwres -r proc --level lpar -m {m} --filter {f} '
                f'-F lpar_name:curr_procs:curr_proc_units:curr_sharing_mode:'
                f'curr_min_procs:curr_max_procs:curr_min_proc_units:curr_max_proc_units:'
                f'run_procs:pend_procs:run_uncap_weight'
            ),
            "mem": (
                f'lshwres -r mem --level lpar -m {m} --filter {f} '
                f'-F lpar_name:curr_mem:run_mem:pend_mem:run_min_mem:curr_max_mem'
            ),
            "veth": (
                f'lshwres -r virtualio --rsubtype eth --level lpar -m {m} --filter {f} '
                f'-F lpar_name:slot_num:port_vlan_id:addl_vlan_ids:ieee_virtual_eth:mac_addr'
            ),
            "vscsi": (
                f'lshwres -r virtualio --rsubtype scsi --level lpar -m {m} --filter {f} '
                f'-F lpar_name:slot_num:remote_lpar_name:remote_slot_num'
            ),
            "vfc": (
                f'lshwres -r virtualio --rsubtype fc --level lpar -m {m} --filter {f} '
                f'-F lpar_name:slot_num:wwpns:remote_lpar_name:remote_slot_num'
            ),
            "phyio": (
                f'lshwres -r io --rsubtype slot -m {m} --filter {f} '
                f'-F lpar_name:drc_name:description:feature_codes'
            ),
            # Physical Fibre Channel HBAs assigned to the LPAR.
            # lshwres -r io --rsubtype slot returns the physical location
            # (drc_name / phys_loc) and description of every physical adapter
            # owned by the partition; we later filter for Fibre Channel ones
            # and pull each port's WWPN from lsnportlogin.
            "phyfc": (
                f'lshwres -r io --rsubtype slot -m {m} --filter {f} '
                f'-F lpar_name:drc_index:drc_name:description:phys_loc'
            )
        }
    )
    cpu_r    = raw["cpu"]
    mem_r    = raw["mem"]
    veth_r   = raw["veth"]
    vscsi_r  = raw["vscsi"]
    vfc_r    = raw["vfc"]
    phyio_r  = raw["phyio"]
    phyfc_r  = raw["phyfc"]


    def parse_lines(res, fields):
        """Parse colon-separated lines; skip lines that start with error text."""
        if not res.get("ok"):
            return []
        rows = []
        for line in res["output"].splitlines():
            line = line.strip()
            if not line:
                continue
            # Skip header or error lines that don't look like data
            if line.startswith("No results") or line.lower().startswith("error"):
                continue
            parts = line.split(":")
            rows.append(dict(zip(fields, parts)))
        return rows

    cpu_fields    = ["lpar_name","curr_procs","curr_proc_units","curr_sharing_mode",
                     "curr_min_procs","curr_max_procs","curr_min_proc_units","curr_max_proc_units",
                     "run_procs","pend_procs","uncap_weight"]
    mem_fields    = ["lpar_name","curr_mem","run_mem","pend_mem","min_mem","max_mem"]
    veth_fields   = ["lpar_name","slot","pvid","addl_vlans","ieee","mac"]
    vscsi_fields  = ["lpar_name","slot","remote_lpar","remote_slot"]
    vfc_fields    = ["lpar_name","slot","wwpns","remote_lpar","remote_slot"]
    phyio_fields  = ["lpar_name","drc_name","description","feature_codes"]
    phyfc_fields  = ["lpar_name","drc_index","drc_name","description","phys_loc"]

    def parse_phyfc(res):
        """Parse physical I/O slots and keep only Fibre Channel HBAs.

        The HMC ``lshwres -r io --rsubtype slot`` output gives us the physical
        location code (``drc_name`` / ``phys_loc``) and the adapter description
        for every physical adapter owned by the partition.  We keep only the
        Fibre Channel ones.  The port WWPN is *not* available from lshwres and
        is filled in later (see ``_enrich_phyfc_wwpn``) by querying the VIOS.

        Returns a list of dicts shaped for the frontend:
            {lpar_name, adapter_id, port_index, location, description, wwpn}
        """
        rows = parse_lines(res, phyfc_fields)
        out = []
        for i, r in enumerate(rows):
            desc = (r.get("description") or "").lower()
            # Only keep physical Fibre Channel adapters
            if "fibre" not in desc and "fiber" not in desc and "fc" not in desc:
                continue
            location = r.get("phys_loc") or r.get("drc_name") or ""
            # Derive a stable adapter id / port index from the location code.
            # e.g. U78D5.001.ABC-P1-C2-T1  → adapter "C2", port "T1"
            adapter_id = ""
            port_index = ""
            for token in location.split("-"):
                token = token.strip()
                if token.upper().startswith("C") and token[1:].isdigit():
                    adapter_id = token
                elif token.upper().startswith("T") and token[1:].isdigit():
                    port_index = token
            out.append({
                "lpar_name":   r.get("lpar_name", ""),
                "adapter_id":  adapter_id or r.get("drc_index", str(i)),
                "port_index":  port_index or "—",
                "location":    location or "—",
                "description": r.get("description", ""),
                "wwpn":        "",   # filled in by _enrich_phyfc_wwpn (World Wide Port Name)
                "port_speed":  "",   # filled in by _enrich_phyfc_wwpn
                "network_address": "",  # filled in by _enrich_phyfc_wwpn (lsdev -vpd)
            })
        return out


    def _norm_loc(loc):
        """Normalise a physical location code to its card slot key ("C<n>") for
        matching between the HMC (lshwres phys_loc/drc_name) and the VIOS
        (lsdev -vpd "Hardware Location Code").

        The HMC side is often reported as just the slot label (e.g. ``C11``)
        while the VIOS reports the full code (e.g.
        ``U78C9.001.WXYZ123-P1-C11-T1``).  Both always contain the ``C<n>``
        card token, so we key on that.  We also keep any ``T<n>`` port token
        so a multi-port card's ports don't collide.
        """
        loc = (loc or "").strip().upper()
        card = ""
        for token in loc.replace(".", "-").split("-"):
            token = token.strip()
            if token.startswith("C") and token[1:].isdigit():
                card = token
        if card:
            return card
        # Fall back to the '-P...' tail if no card token was found.
        idx = loc.find("-P")
        return loc[idx:] if idx != -1 else loc



    # Captures the VIOS enrichment queries so they appear in the ?debug=1 view.
    phyfc_debug = {}

    def _enrich_phyfc_wwpn(phyfc_rows):

        """Fill in each physical FC port's WWPN, link status, port speed and
        network address by querying the VIOS.


        Collection method (as confirmed on the VIOS), equivalent to::

            for i in $(viosvrcmd -m <ms> -p <VIOS> \\
                          -c "lsdev -type adapter" | grep fcs | grep Available \\
                          | cut -d' ' -f1); do
                viosvrcmd -m <ms> -p <VIOS> -t 1 -c "fcstat -d $i" \\
                    | grep -E "World Wide Port Name|Attention Type|Port Speed"
                viosvrcmd -m <ms> -p <VIOS> -c "lsdev -dev $i -vpd" \\
                    | grep -E "Hardware Location Code|Network Address"
            done

        The ``fcstat -d fcsX`` output contains::

            World Wide Port Name: 0x100000109B5A1234   <-- WWPN (port name)
            Attention Type:   Link Up                  <-- link status
            Port Speed (running):   16 GBIT            <-- negotiated speed

        The ``lsdev -dev fcsX -vpd`` output contains::

            Hardware Location Code......U78..-P1-C2-T1  <-- physical location
            Network Address.............100000109B5A1234 <-- WWPN-format address

        We:
          1. list the Available fcs* adapters on the VIOS
             (lsdev -type adapter | grep fcs | grep Available),
          2. run ``fcstat -d fcsX`` (WWPN + link + speed) and
             ``lsdev -dev fcsX -vpd`` (location code + network address) for
             each device over a single batched SSH connection,
          3. match each device's location code to the lshwres phyfc row and
             copy in the World Wide Port Name, link status, port speed and
             network address.
        """

        if not phyfc_rows:
            return phyfc_rows

        # The detail view is per-LPAR; treat that LPAR as the VIOS to query.
        vios = lpar_id

        # ── 1. list Available fcs* adapters on the VIOS ──
        # viosvrcmd -m <ms> -p <VIOS> -c "lsdev -type adapter" | grep fcs | grep Available
        lsdev_cmd = (f'viosvrcmd -m {m} -p "{vios}" '
                     f'-c "lsdev -type adapter" | grep fcs | grep Available')
        lsdev_res = ssh_manager.run_hmc_command(
            host=hmc["host"], port=int(hmc.get("ssh_port", 22)),
            username=hmc.get("username", "hscroot"),
            key_path=hmc.get("key_path") or None,
            command=lsdev_cmd,
        )
        # Expose the enrichment queries in the debug view for diagnosis.
        phyfc_debug["phyfc_vios_lsdev (list fcs adapters)"] = {
            "ok": lsdev_res.get("ok", False),
            "output": lsdev_res.get("output", ""),
            "error": lsdev_res.get("error") or lsdev_res.get("stderr") or "",
            "command": lsdev_cmd,
        }

        fcs_devs = []
        if lsdev_res.get("ok"):
            for line in lsdev_res["output"].splitlines():
                tok = line.strip().split()
                if tok and tok[0].lower().startswith("fcs"):
                    if tok[0] not in fcs_devs:
                        fcs_devs.append(tok[0])
        if not fcs_devs:
            return phyfc_rows

        # ── 2. fcstat -d (WWPN + link + speed) and lsdev -vpd (location +
        #        network address) per device, in one batched SSH connection.
        #
        #   viosvrcmd -m <ms> -p <VIOS> -t 1 -c "fcstat -d fcsX"
        #       | grep -E "World Wide Port Name|Attention Type|Port Speed"
        #   viosvrcmd -m <ms> -p <VIOS> -c "lsdev -dev fcsX -vpd"
        #       | grep -E "Hardware Location Code|Network Address"
        batch_cmds = {}
        for dev in fcs_devs:
            batch_cmds[f"stat::{dev}"] = (
                f'viosvrcmd -m {m} -p "{vios}" -t 1 '
                f'-c "fcstat {dev}" '
                f'| grep -E "World Wide Port Name|Attention Type|Port Speed"')

            batch_cmds[f"vpd::{dev}"] = (
                f'viosvrcmd -m {m} -p "{vios}" '
                f'-c "lsdev -dev {dev} -vpd" '
                f'| grep -E "Hardware Location Code|Network Address"')
        batch_raw = ssh_manager.run_hmc_commands_batch(
            host=hmc["host"], port=int(hmc.get("ssh_port", 22)),
            username=hmc.get("username", "hscroot"),
            key_path=hmc.get("key_path") or None,
            commands=batch_cmds,
        )
        # "fcstat fcsX" returns the World Wide Port Name / Attention Type /
        # Port Speed only when the port is UP.  For a port whose link is DOWN
        # the plain command times out / returns nothing, so we retry those
        # devices with "fcstat -d fcsX" (diagnostic mode) which still reports
        # the WWPN and link status for a down port.
        retry_cmds = {}
        for dev in fcs_devs:
            r = batch_raw.get(f"stat::{dev}", {})
            if not r.get("ok") or not (r.get("output") or "").strip():
                retry_cmds[f"stat::{dev}"] = (
                    f'viosvrcmd -m {m} -p "{vios}" '
                    f'-c "fcstat -d {dev}" '
                    f'| grep -E "World Wide Port Name|Attention Type|Port Speed"')

        if retry_cmds:
            retry_raw = ssh_manager.run_hmc_commands_batch(
                host=hmc["host"], port=int(hmc.get("ssh_port", 22)),
                username=hmc.get("username", "hscroot"),
                key_path=hmc.get("key_path") or None,
                commands=retry_cmds,
            )
            for _k, _res in retry_raw.items():
                if _res.get("ok") and (_res.get("output") or "").strip():
                    batch_raw[_k] = _res
                    batch_cmds[f"{_k} (retry fcstat -d)"] = retry_cmds[_k]


        # Expose each fcstat / lsdev-vpd query in the debug view.
        for _k, _cmd in batch_cmds.items():
            _r = batch_raw.get(_k, {})
            phyfc_debug[f"phyfc_vios {_k}"] = {
                "ok": _r.get("ok", False),
                "output": _r.get("output", ""),
                "error": _r.get("error") or _r.get("stderr") or "",
                "command": _cmd,
            }


        # ── 3. per device: parse WWPN/link/speed (fcstat) + location/network
        #        address (lsdev -vpd)

        loc_to_info = {}
        for dev in fcs_devs:
            # --- fcstat -d: World Wide Port Name / Attention Type / Port Speed
            wwpn = ""
            link = ""
            speed = ""
            speed_running = ""
            speed_supported = ""
            stat_res = batch_raw.get(f"stat::{dev}", {})
            if stat_res.get("ok"):
                for line in stat_res["output"].splitlines():
                    s = line.strip()
                    low = s.lower()
                    if "world wide port name" in low:
                        val = s.split(":", 1)[-1].strip()
                        # strip a leading 0x and keep hex digits only
                        val = val[2:] if val.lower().startswith("0x") else val
                        wwpn = "".join(ch for ch in val
                                       if ch in "0123456789abcdefABCDEF")
                    elif "attention type" in low:
                        link = s.split(":", 1)[-1].strip()
                    elif "port speed" in low:
                        # fcstat reports two "Port Speed" lines:
                        #   Port Speed (supported): 32 GBIT
                        #   Port Speed (running):   16 GBIT   (0 GBIT when down)
                        val = s.split(":", 1)[-1].strip()
                        if "running" in low:
                            speed_running = val
                        elif "supported" in low:
                            speed_supported = val
                        else:
                            speed = val
                # Prefer the running speed; if the link is down (running 0 GBIT
                # or empty) show the supported speed so the panel is never blank.
                def _is_zero(v):
                    return (not v) or v.strip().split()[0] in ("0", "0.0")
                if speed_running and not _is_zero(speed_running):
                    speed = speed_running
                elif speed_supported:
                    # Link down: show supported speed, annotated.
                    speed = (f"{speed_supported} (supported)"
                             if _is_zero(speed_running) else speed_supported)
                elif speed_running:
                    speed = speed_running


            # --- lsdev -vpd: Hardware Location Code / Network Address
            # VPD lines use a dotted-fill label style, e.g.
            #   Hardware Location Code......U78D5.001.ABC-P1-C11-T1
            #   Network Address.............100000109B5A1234
            # Split the label from the value on the run of 2+ dots.
            dev_loc = ""
            net_addr = ""
            vpd_res = batch_raw.get(f"vpd::{dev}", {})
            if vpd_res.get("ok"):
                for line in vpd_res["output"].splitlines():
                    s = line.strip()
                    low = s.lower()
                    if "hardware location code" in low:
                        parts = re.split(r"\.{2,}", s, maxsplit=1)
                        dev_loc = (parts[-1] if len(parts) > 1
                                   else s.split(":", 1)[-1]).strip()
                    elif "network address" in low:
                        parts = re.split(r"\.{2,}", s, maxsplit=1)
                        val = (parts[-1] if len(parts) > 1
                               else s.split(":", 1)[-1]).strip()
                        net_addr = "".join(ch for ch in val
                                           if ch in "0123456789abcdefABCDEF")



            # Fall back to the Network Address (a WWPN-format value) if fcstat
            # did not return a World Wide Port Name.
            if not wwpn and net_addr:
                wwpn = net_addr

            if dev_loc:
                # A single FC card (e.g. C7) usually has multiple ports
                # (T0/T1), each with its own device / WWPN.  Group ports by
                # the card slot key so all ports of a card can be shown.
                # Derive the port token (T<n>) from the VPD location code.
                port_token = ""
                for t in dev_loc.replace(".", "-").split("-"):
                    t = t.strip()
                    if t.upper().startswith("T") and t[1:].isdigit():
                        port_token = t.upper()
                loc_to_info.setdefault(_norm_loc(dev_loc), []).append({
                    "dev": dev, "wwpn": wwpn, "link": link,
                    "speed": speed, "net_addr": net_addr,
                    "port": port_token, "location": dev_loc,
                })

        # ── 4. merge WWPN + link + speed + network address into the lshwres
        #        phyfc rows, matched by physical card location code.  Each row
        #        carries a "ports" list (one entry per physical port on the
        #        card) plus flat fields (first port) for backward compatibility.
        for row in phyfc_rows:
            ports = loc_to_info.get(_norm_loc(row.get("location", "")))
            if ports:
                # Sort ports by their T<n> token for stable display order.
                ports_sorted = sorted(ports, key=lambda p: p.get("port", ""))
                row["ports"] = ports_sorted
                first = ports_sorted[0]
                row["wwpn"]            = first["wwpn"]
                row["device"]          = first["dev"]
                row["link_status"]     = first["link"]
                row["port_speed"]      = first["speed"]
                row["network_address"] = first["net_addr"]
        return phyfc_rows





    # Collect per-resource errors (None = success/no-data, string = error msg)
    raw_results = [
        ("cpu",    cpu_r),  ("mem",  mem_r),
        ("veth",   veth_r), ("vscsi",vscsi_r), ("vfc",  vfc_r),
        ("phyio", phyio_r), ("phyfc", phyfc_r)
    ]
    errors = {k: v.get("error") for k, v in raw_results if not v.get("ok")}


    # Always include raw output in debug mode so the UI can show it
    debug = request.args.get("debug") == "1"

    # Build the physical FC list from lshwres, then enrich each port's WWPN
    # by querying the VIOS with "lscfg -vl fcsX" (matched by location code).
    phyfc_rows = _enrich_phyfc_wwpn(parse_phyfc(phyfc_r))

    result = {
        "ok":    True,
        "cpu":   parse_lines(cpu_r,    cpu_fields),
        "mem":   parse_lines(mem_r,    mem_fields),
        "veth":  parse_lines(veth_r,   veth_fields),
        "vscsi": parse_lines(vscsi_r,  vscsi_fields),
        "vfc":   parse_lines(vfc_r,    vfc_fields),
        "phyio": parse_lines(phyio_r, phyio_fields),
        "phyfc": phyfc_rows,
        "errors": errors,
    }

    if debug:
        result["raw"] = {k: v for k, v in raw.items()}
        # Include the VIOS enrichment queries (lsdev / fcstat / lsdev -vpd)
        # so the raw output behind the Physical FC HBAs panel is visible.
        result["raw"].update(phyfc_debug)
    return jsonify(result)




@app.route("/api/hmcs/<hmc_id>/managed-systems/<system_id>/vfc-topology",
           methods=["GET"])
def get_vfc_topology(hmc_id, system_id):
    """Return the Virtual Fibre Channel (NPIV) topology for a managed system.

    Runs, over a single SSH connection to the HMC:
      1. lshwres -r virtualio --rsubtype fc --level lpar -m <system>
         → every virtual FC adapter mapping (client + server side).
      2. viosvrcmd -m <system> -p <VIOS> -c "lsmap -all -npiv -fmt ,"
         → the vfchost ↔ fcs ↔ client mapping as seen from each VIOS.

    The raw command text is returned verbatim so the UI can render it above
    the topology graph, together with parsed rows used to draw the graph.
    """
    hmc = hmc_store.get(hmc_id)
    if not hmc:
        return jsonify({"ok": False, "error": "HMC not found"}), 404

    m = f'"{system_id}"'

    # ── Step 1: lshwres for every virtual FC adapter on the system ──
    lshwres_cmd = (
        f'lshwres -r virtualio --rsubtype fc --level lpar -m {m} '
        f'-F lpar_name:lpar_id:slot_num:adapter_type:remote_lpar_name:'
        f'remote_lpar_id:remote_slot_num:wwpns'
    )
    lshwres_res = ssh_manager.run_hmc_command(
        host=hmc["host"], port=int(hmc.get("ssh_port", 22)),
        username=hmc.get("username", "hscroot"),
        key_path=hmc.get("key_path") or None,
        command=lshwres_cmd,
    )

    # Parse lshwres rows and discover the VIOS partitions (adapter_type=server)
    fc_fields = ["lpar_name", "lpar_id", "slot_num", "adapter_type",
                 "remote_lpar_name", "remote_lpar_id", "remote_slot_num", "wwpns"]
    fc_rows = []
    vios_names = []
    if lshwres_res.get("ok"):
        for line in lshwres_res["output"].splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("No results") or line.lower().startswith("error"):
                continue
            parts = line.split(":")
            row = dict(zip(fc_fields, parts))
            fc_rows.append(row)
            if row.get("adapter_type") == "server" and row.get("lpar_name"):
                if row["lpar_name"] not in vios_names:
                    vios_names.append(row["lpar_name"])

    # ── Step 2: lsmap -all -npiv on each discovered VIOS ──
    lsmap_cmds = {
        name: (f'viosvrcmd -m {m} -p "{name}" '
               f'-c "lsmap -all -npiv -fmt ,"')
        for name in vios_names
    }
    lsmap_raw = {}
    if lsmap_cmds:
        lsmap_raw = ssh_manager.run_hmc_commands_batch(
            host=hmc["host"], port=int(hmc.get("ssh_port", 22)),
            username=hmc.get("username", "hscroot"),
            key_path=hmc.get("key_path") or None,
            commands=lsmap_cmds,
        )

    # lsmap -fmt , output columns (comma-separated):
    #   1st value  = vfchost name/number (server virtual FC adapter)
    #   2nd value  = physloc of the vfchost; the number after "-C" is the
    #                VIOS slot number that hosts this vfchost
    #   ...
    #   last value = physloc of the remote (client) virtual FC adapter; the
    #                number after "-C" is the client LPAR slot number
    # name,physloc,clntid,clntname,clntos,status,fc,fcphysloc,ports,flags,vfcclient,vfcclientdrc
    lsmap_fields = ["vfchost", "physloc", "clntid", "clntname", "clntos",
                    "status", "fc", "fcphysloc", "ports", "flags",
                    "vfcclient", "vfcclientdrc"]

    def slot_after_c(physloc: str) -> str:
        """Extract the slot number that follows '-C' in a physical location code.

        e.g. 'U8286.42A.XXXXXXX-V2-C12' -> '12'. Returns '' if not present.
        """
        if not physloc:
            return ""
        for token in physloc.split("-"):
            token = token.strip()
            if token.upper().startswith("C") and token[1:].isdigit():
                return token[1:]
        return ""

    lsmap_parsed = {}
    for name, res in lsmap_raw.items():
        rows = []
        if res.get("ok"):
            for line in res["output"].splitlines():
                line = line.strip()
                if not line or line.startswith(("name,", "No results")):
                    continue
                if line.lower().startswith("error"):
                    continue
                parts = line.split(",")
                row = dict(zip(lsmap_fields, parts))
                # Strip whitespace from every field so that blank/space-only
                # values (e.g. clntname=" " for NOT_LOGGED_IN entries) are
                # normalised to empty strings and won't create phantom nodes.
                for k in row:
                    row[k] = row[k].strip()
                # Derive the VIOS slot (from vfchost physloc) and the client
                # LPAR slot (from the remote/client vfc physloc).
                row["vios_slot"] = slot_after_c(row.get("physloc", ""))
                row["client_slot"] = slot_after_c(
                    row.get("vfcclientdrc") or row.get("fcphysloc") or "")
                rows.append(row)
        lsmap_parsed[name] = rows


    # ── Assemble raw command text for display above the graph ──
    commands = []
    commands.append({
        "title": "lshwres — virtual FC adapters",
        "command": lshwres_cmd,
        "output": (lshwres_res.get("output") if lshwres_res.get("ok")
                   else lshwres_res.get("error", "command failed")) or "(no output)",
        "ok": bool(lshwres_res.get("ok")),
    })
    for name in vios_names:
        res = lsmap_raw.get(name, {})
        commands.append({
            "title": f"lsmap -all -npiv on VIOS {name}",
            "command": lsmap_cmds[name],
            "output": (res.get("output") if res.get("ok")
                       else res.get("error", "command failed")) or "(no output)",
            "ok": bool(res.get("ok")),
        })

    return jsonify({
        "ok": True,
        "system_id": system_id,
        "vios": vios_names,
        "fc_adapters": fc_rows,
        "lsmap": lsmap_parsed,
        "commands": commands,
    })


@app.route("/api/hmcs/<hmc_id>/managed-systems/<system_id>/vscsi-topology",
           methods=["GET"])
def get_vscsi_topology(hmc_id, system_id):
    """Return the virtual SCSI topology for a managed system.

    Runs, over a single SSH connection to the HMC:
      1. lshwres -r virtualio --rsubtype scsi --level lpar -m <system>
         → every vSCSI adapter mapping (server VIOS side + client LPAR side).
      2. viosvrcmd -m <system> -p <VIOS> -c "lsmap -all -fmt ,"
         → the vhostX ↔ backing-device ↔ client-slot mapping from each VIOS.

    JOIN KEY: lshwres row slot_num (server adapter) == lsmap row svr_slot
    The joined data drives the VIOS → vSCSI Target → Client LPAR graph.
    """
    hmc = hmc_store.get(hmc_id)
    if not hmc:
        return jsonify({"ok": False, "error": "HMC not found"}), 404

    m = f'"{system_id}"'

    # ── Step 1: lshwres for every virtual SCSI adapter on the system ──
    lshwres_cmd = (
        f'lshwres -r virtualio --rsubtype scsi --level lpar -m {m} '
        f'-F lpar_name:lpar_id:slot_num:adapter_type:'
        f'remote_lpar_name:remote_lpar_id:remote_slot_num'
    )
    lshwres_res = ssh_manager.run_hmc_command(
        host=hmc["host"], port=int(hmc.get("ssh_port", 22)),
        username=hmc.get("username", "hscroot"),
        key_path=hmc.get("key_path") or None,
        command=lshwres_cmd,
    )

    # Parse lshwres rows; collect VIOS names from server-side adapter rows
    scsi_fields = ["lpar_name", "lpar_id", "slot_num", "adapter_type",
                   "remote_lpar_name", "remote_lpar_id", "remote_slot_num"]
    scsi_rows = []
    vios_names = []
    if lshwres_res.get("ok"):
        for line in lshwres_res["output"].splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("No results") or line.lower().startswith("error"):
                continue
            parts = line.split(":")
            row = dict(zip(scsi_fields, parts))
            scsi_rows.append(row)
            if row.get("adapter_type") == "server" and row.get("lpar_name"):
                if row["lpar_name"] not in vios_names:
                    vios_names.append(row["lpar_name"])

    # ── Step 2: lsmap -all on each discovered VIOS ──
    # lsmap -fmt , output (comma-separated):
    #   vhostX, physloc, svr_vtd, backing_device, backing_type, status
    # The physloc "-C<N>" slot number is the VIOS server-adapter slot (join key).
    lsmap_cmds = {
        name: (f'viosvrcmd -m {m} -p "{name}" '
               f'-c "lsmap -all -fmt ,"')
        for name in vios_names
    }
    lsmap_raw = {}
    if lsmap_cmds:
        lsmap_raw = ssh_manager.run_hmc_commands_batch(
            host=hmc["host"], port=int(hmc.get("ssh_port", 22)),
            username=hmc.get("username", "hscroot"),
            key_path=hmc.get("key_path") or None,
            commands=lsmap_cmds,
        )

    def slot_after_c(physloc: str) -> str:
        """Extract slot number after '-C' in a physical location code."""
        if not physloc:
            return ""
        for token in physloc.split("-"):
            token = token.strip()
            if token.upper().startswith("C") and token[1:].isdigit():
                return token[1:]
        return ""

    # lsmap -all -fmt , columns (space- or comma-separated — actual order varies
    # by VIOS version; the most common layout is):
    #   vhost, physloc, vtd, backing, backing_type, status, [clntid, clntname, ...]
    lsmap_fields = ["vhost", "physloc", "vtd", "backing", "backing_type", "status",
                    "clntid", "clntname"]

    lsmap_parsed = {}
    for name, res in lsmap_raw.items():
        rows = []
        if res.get("ok"):
            for line in res["output"].splitlines():
                line = line.strip()
                if not line or line.startswith(("vhost,", "No results")):
                    continue
                if line.lower().startswith("error"):
                    continue
                parts = line.split(",")
                row = dict(zip(lsmap_fields, parts))
                # svr_slot = the slot number in the VIOS physloc ("-C<N>")
                # This is the JOIN KEY that matches lshwres slot_num.
                row["svr_slot"] = slot_after_c(row.get("physloc", ""))
                rows.append(row)
        lsmap_parsed[name] = rows

    # ── Assemble raw command text for display above the graph ──
    commands = []
    commands.append({
        "title":   "lshwres — virtual SCSI adapters",
        "command": lshwres_cmd,
        "output":  (lshwres_res.get("output") if lshwres_res.get("ok")
                    else lshwres_res.get("error", "command failed")) or "(no output)",
        "ok":      bool(lshwres_res.get("ok")),
    })
    for name in vios_names:
        res = lsmap_raw.get(name, {})
        commands.append({
            "title":   f"lsmap -all on VIOS {name}",
            "command": lsmap_cmds[name],
            "output":  (res.get("output") if res.get("ok")
                        else res.get("error", "command failed")) or "(no output)",
            "ok":      bool(res.get("ok")),
        })

    return jsonify({
        "ok":            True,
        "system_id":     system_id,
        "vios":          vios_names,
        "scsi_adapters": scsi_rows,
        "lsmap":         lsmap_parsed,
        "commands":      commands,
    })


@app.route("/api/hmcs/<hmc_id>/managed-systems/<system_id>/vnet-topology",
           methods=["GET"])
def get_vnet_topology(hmc_id, system_id):
    """Return the Virtual Network (SEA) topology for a managed system.

    Runs, over a single SSH connection to the HMC:
      1. lshwres -r virtualio --rsubtype eth --level lpar -m <system>
         → every virtual ethernet adapter (client LPARs + VIOS trunk adapters).
      2. viosvrcmd -m <system> -p <VIOS> -c "lsdev -type adapter -field name description state"
         | grep -i shared
         → identifies the SEA device name on each VIOS.
      3. viosvrcmd -m <system> -p <VIOS> -c "entstat -all <sea_device>"
         → extracts Real Adapter, Target Virtual Adapter Slot, link speed/status.

    JOIN KEY (Step 1 ↔ Step 3):
      lshwres server-adapter slot_num  ==  entstat "Target Virtual Adapter Slot"
    """
    hmc = hmc_store.get(hmc_id)
    if not hmc:
        return jsonify({"ok": False, "error": "HMC not found"}), 404

    m = f'"{system_id}"'
    commands_out = []

    # ── Step 1: lshwres for every virtual ethernet adapter ──────────────────
    # Use comma-separated field names and comma as the output field delimiter.
    # Note: there is no adapter_type field in this output format.
    # Trunk (VIOS) adapters are identified by is_trunk=1.
    # Client LPAR adapters have is_trunk=0 or empty.
    lshwres_cmd = (
        f'lshwres -r virtualio --rsubtype eth --level lpar -m {m} '
        f'-F lpar_name,lpar_id,slot_num,vswitch,port_vlan_id,addl_vlan_ids,is_trunk,trunk_priority'
    )
    lshwres_res = ssh_manager.run_hmc_command(
        host=hmc["host"], port=int(hmc.get("ssh_port", 22)),
        username=hmc.get("username", "hscroot"),
        key_path=hmc.get("key_path") or None,
        command=lshwres_cmd,
    )

    # Fields returned (comma-delimited output from -F with comma separators):
    #   lpar_name, lpar_id, slot_num, vswitch, port_vlan_id,
    #   addl_vlan_ids, is_trunk, trunk_priority
    eth_fields = ["lpar_name", "lpar_id", "slot_num", "vswitch",
                  "port_vlan_id", "addl_vlan_ids", "is_trunk", "trunk_priority"]
    eth_rows = []
    vios_names = []
    if lshwres_res.get("ok"):
        for line in lshwres_res["output"].splitlines():
            line = line.strip()
            if not line or line.startswith("No results") or line.lower().startswith("error"):
                continue
            # The -F with comma separator outputs fields separated by commas.
            # addl_vlan_ids may itself contain spaces but not commas, so split is safe.
            parts = line.split(",")
            row = dict(zip(eth_fields, parts))
            # Derive adapter_type from is_trunk: "1" = server/VIOS trunk, else client
            row["adapter_type"] = "server" if row.get("is_trunk") == "1" else "client"
            eth_rows.append(row)
            # Trunk adapters (is_trunk=1) are hosted on VIO Servers
            if row.get("is_trunk") == "1" and row.get("lpar_name"):
                if row["lpar_name"] not in vios_names:
                    vios_names.append(row["lpar_name"])

    commands_out.append({
        "title": "lshwres — virtual ethernet adapters (Step 1)",
        "command": lshwres_cmd,
        "output": (lshwres_res.get("output") if lshwres_res.get("ok")
                   else lshwres_res.get("error", "command failed")) or "(no output)",
        "ok": bool(lshwres_res.get("ok")),
    })

    # ── Step 2: lsdev on each VIOS, pipe through grep -i shared ─────────────
    # Command: viosvrcmd -m <ms> -p <VIOS> -c "lsdev" | grep -i shared
    # The first column of each matching line is the SEA device name (e.g. ent5).
    lsdev_cmds = {
        name: f'viosvrcmd -m {m} -p "{name}" -c "lsdev" | grep -i shared'
        for name in vios_names
    }
    lsdev_raw = {}
    if lsdev_cmds:
        lsdev_raw = ssh_manager.run_hmc_commands_batch(
            host=hmc["host"], port=int(hmc.get("ssh_port", 22)),
            username=hmc.get("username", "hscroot"),
            key_path=hmc.get("key_path") or None,
            commands=lsdev_cmds,
        )

    # Parse: each output line is already filtered to "shared" entries.
    # The first whitespace-delimited token is the device name (entX).
    sea_devices = {}   # vios_name → list of sea device names (e.g. ["ent5"])
    for vios, res in lsdev_raw.items():
        devices = []
        if res.get("ok"):
            for line in res["output"].splitlines():
                line = line.strip()
                if not line:
                    continue
                parts = line.split()
                if parts:
                    dev = parts[0]
                    # Sanity-check: device name should look like entX
                    if dev.startswith("ent") and dev not in devices:
                        devices.append(dev)
        sea_devices[vios] = devices
        commands_out.append({
            "title": f"lsdev (grep shared) on VIOS {vios} — find SEA (Step 2)",
            "command": lsdev_cmds[vios],
            "output": (res.get("output") if res.get("ok")
                       else res.get("error", "command failed")) or "(no output)",
            "ok": bool(res.get("ok")),
        })

    # ── Step 3: entstat grep commands per SEA device ─────────────────────────
    # Run targeted grep-filtered entstat commands to extract specific fields
    # reliably from the dense entstat -all output.
    #
    # Fields extracted (matching the user-confirmed real output format):
    #   Real Adapter         → backing physical adapter (e.g. ent0)
    #   Physical Port Link Status → physical link state (Up/Down)
    #   Logical Port Link Status  → logical (SEA) link state
    #   Physical Port Speed  → link speed (e.g. "1Gbps Full Duplex")
    #   Virtual Adapter      → VIOS trunk virtual adapter (e.g. ent4 = trunk slot)
    #   Port VLAN ID         → PVID carried by the trunk adapter
    #   VLAN Tag IDs         → tagged VLANs bridged by the SEA

    # Build one batch of grep commands per SEA device per field.
    # Key format: "<vios>::<dev>::<field_key>"
    entstat_grep_cmds = {}
    for vios, devices in sea_devices.items():
        for dev in devices:
            base = f'viosvrcmd -m {m} -p "{vios}" -c "entstat -all {dev}"'
            prefix = f"{vios}::{dev}"
            entstat_grep_cmds[f"{prefix}::real_adapter"]    = f'{base} |grep -i "Real Adapter"'
            entstat_grep_cmds[f"{prefix}::phys_link"]       = f'{base} |grep -i "Physical Port Link Status"'
            entstat_grep_cmds[f"{prefix}::logical_link"]    = f'{base} |grep -i "Logical Port Link Status"'
            entstat_grep_cmds[f"{prefix}::phys_speed"]      = f'{base} |grep -i "Physical Port Speed"'
            entstat_grep_cmds[f"{prefix}::virtual_adapter"] = f'{base} |grep -i "Virtual Adapter"'
            entstat_grep_cmds[f"{prefix}::port_vlan"]       = f'{base} |grep -i "Port VLAN ID"'
            entstat_grep_cmds[f"{prefix}::vlan_tag_ids"]    = f'{base} |grep -i "VLAN Tag IDs"'

    entstat_grep_raw = {}
    if entstat_grep_cmds:
        entstat_grep_raw = ssh_manager.run_hmc_commands_batch(
            host=hmc["host"], port=int(hmc.get("ssh_port", 22)),
            username=hmc.get("username", "hscroot"),
            key_path=hmc.get("key_path") or None,
            commands=entstat_grep_cmds,
        )

    def _first_val(output: str, key_prefix: str) -> str:
        """Return the value after the first ':' on the first matching output line."""
        for line in (output or "").splitlines():
            line = line.strip()
            if ":" in line:
                return line.split(":", 1)[-1].strip()
        return ""

    def _all_vals(output: str) -> list:
        """Return all values after ':' for repeated fields (e.g. Port VLAN ID)."""
        vals = []
        for line in (output or "").splitlines():
            line = line.strip()
            if ":" in line:
                v = line.split(":", 1)[-1].strip()
                if v and v not in vals:
                    vals.append(v)
        return vals

    # Assemble per-device info dicts and build command records for the panel
    entstat_parsed = {}   # vios::dev → info dict

    # Track which (vios, dev) pairs we've processed to avoid duplicate panel entries
    processed_keys = set()

    for field_key, res in entstat_grep_raw.items():
        parts = field_key.split("::")
        if len(parts) < 3:
            continue
        vios, dev, field = parts[0], parts[1], parts[2]
        sea_key = f"{vios}::{dev}"
        if sea_key not in entstat_parsed:
            entstat_parsed[sea_key] = {
                "real_adapter": "", "phys_link": "", "logical_link": "",
                "phys_speed": "", "virtual_adapter": "", "port_vlan": "",
                "vlan_tag_ids": "",
                # legacy aliases used by buildVnetFromLive
                "link_speed": "", "link_status": "", "trunk_slot": "",
                "phys_ports": "", "bridged_vlans": "",
            }
        info = entstat_parsed[sea_key]
        out  = res.get("output", "") if res.get("ok") else ""

        if field == "real_adapter":
            info["real_adapter"] = _first_val(out, "Real Adapter")
        elif field == "phys_link":
            info["phys_link"] = _first_val(out, "Physical Port Link Status")
            info["link_status"] = info["phys_link"]
        elif field == "logical_link":
            info["logical_link"] = _first_val(out, "Logical Port Link Status")
        elif field == "phys_speed":
            info["phys_speed"] = _first_val(out, "Physical Port Speed")
            info["link_speed"] = info["phys_speed"]
        elif field == "virtual_adapter":
            info["virtual_adapter"] = _first_val(out, "Virtual Adapter")
            # Virtual Adapter (e.g. ent4) maps to the VIOS trunk slot.
            # Cross-reference with lshwres trunk rows to find slot_num.
            va = info["virtual_adapter"]
            if va:
                # Try to find the trunk lshwres row for this VIOS whose
                # vswitch adapter name matches the virtual adapter.
                # As a fallback, trunk_slot stays empty — JS uses lshwres slot_num.
                trunk_rows = [r for r in eth_rows
                              if r.get("lpar_name") == vios and r.get("is_trunk") == "1"]
                if trunk_rows:
                    info["trunk_slot"] = trunk_rows[0].get("slot_num", "")
        elif field == "port_vlan":
            vals = _all_vals(out)
            info["port_vlan"] = ", ".join(vals) if vals else ""
        elif field == "vlan_tag_ids":
            info["vlan_tag_ids"] = _first_val(out, "VLAN Tag IDs")
            # Populate bridged_vlans alias from tag IDs (or port VLAN if no tags)
            if info["vlan_tag_ids"] and info["vlan_tag_ids"].lower() != "none":
                info["bridged_vlans"] = info["vlan_tag_ids"]
            elif info.get("port_vlan"):
                info["bridged_vlans"] = info["port_vlan"]

    # Build command output entries for the panel — one block per SEA device
    # showing all 7 grep commands and their output together.
    for vios, devices in sea_devices.items():
        for dev in devices:
            sea_key = f"{vios}::{dev}"
            field_labels = [
                ("real_adapter",    "Real Adapter"),
                ("phys_link",       "Physical Port Link Status"),
                ("logical_link",    "Logical Port Link Status"),
                ("phys_speed",      "Physical Port Speed"),
                ("virtual_adapter", "Virtual Adapter"),
                ("port_vlan",       "Port VLAN ID"),
                ("vlan_tag_ids",    "VLAN Tag IDs"),
            ]
            # Show each grep command + its output as a separate command block
            for field, label in field_labels:
                fkey = f"{sea_key}::{field}"
                cmd_str = entstat_grep_cmds.get(fkey, "")
                res = entstat_grep_raw.get(fkey, {})
                commands_out.append({
                    "title": f"entstat {dev} · {label} (Step 3, VIOS {vios})",
                    "command": cmd_str,
                    "output": (res.get("output") if res.get("ok")
                               else res.get("error", "command failed")) or "(no output)",
                    "ok": bool(res.get("ok")),
                })

    return jsonify({
        "ok": True,
        "system_id": system_id,
        "vios": vios_names,
        "eth_adapters": eth_rows,
        "sea_devices": sea_devices,
        "entstat": entstat_parsed,
        "commands": commands_out,
    })


@app.route("/api/hmcs/<hmc_id>/managed-systems/<system_id>/lpars/<path:lpar_id>/profiles",
           methods=["GET"])
def lpar_profiles(hmc_id, system_id, lpar_id):
    """Return the list of profile names defined for a given LPAR via SSH lssyscfg."""
    hmc = hmc_store.get(hmc_id)
    if not hmc:
        return jsonify({"ok": False, "error": "HMC not found"}), 404

    cmd = (f'lssyscfg -m "{system_id}" -r prof '
           f'--filter "lpar_names={lpar_id}" -F name')
    result = ssh_manager.run_hmc_command(
        host=hmc["host"],
        port=int(hmc.get("ssh_port", 22)),
        username=hmc.get("username", "hscroot"),
        key_path=hmc.get("key_path") or None,
        command=cmd,
    )

    if not result.get("ok"):
        return jsonify({
            "ok": False,
            "error": (result.get("error") or result.get("stderr")
                      or "lssyscfg failed").strip(),
            "command": cmd,
        }), 400

    profiles = []
    for line in result.get("output", "").splitlines():
        line = line.strip()
        if not line or line.startswith("No results") or line.lower().startswith("error"):
            continue
        if line not in profiles:
            profiles.append(line)

    return jsonify({"ok": True, "data": profiles, "command": cmd})


@app.route("/api/hmcs/<hmc_id>/managed-systems/<system_id>/lpars/<path:lpar_id>/action",
           methods=["POST"])
def lpar_action(hmc_id, system_id, lpar_id):


    hmc = hmc_store.get(hmc_id)
    if not hmc:
        return jsonify({"error": "HMC not found"}), 404
    data = request.get_json(force=True) or {}
    action = data.get("action")  # activate | shutdown | restart | softstop
    if not action:
        return jsonify({"error": "action required"}), 400

    # lpar_id from the UI is actually the LPAR name (see get_lpars: id = name).
    lpar_name = lpar_id

    # Build the chsysstate command based on the requested action.
    if action == "activate":
        profile_name = (data.get("profile_name") or "").strip()
        # boot mode: normal | sms | dd (diagnostic) | of (open firmware)
        boot_mode = (data.get("mode") or "norm").strip()
        # Normalise a few friendly aliases to the chsysstate -b values.
        mode_map = {
            "normal": "norm",
            "norm":   "norm",
            "sms":    "sms",
            "dd":     "dd",
            "of":     "of",
            "open_firmware": "of",
        }
        boot_mode = mode_map.get(boot_mode.lower(), boot_mode)

        cmd = (f'chsysstate -m "{system_id}" -o on -n "{lpar_name}" '
               f'-r lpar -b {boot_mode}')
        if profile_name:
            cmd += f' -f "{profile_name}"'
    elif action == "shutdown":
        # Hard power off (immediate)
        cmd = (f'chsysstate -m "{system_id}" -o shutdown -n "{lpar_name}" '
               f'-r lpar --immed')
    elif action == "softstop":
        # Graceful OS shutdown
        cmd = (f'chsysstate -m "{system_id}" -o osshutdown -n "{lpar_name}" '
               f'-r lpar')
    elif action == "restart":
        # Immediate restart
        cmd = (f'chsysstate -m "{system_id}" -o shutdown -n "{lpar_name}" '
               f'-r lpar --immed --restart')
    else:
        return jsonify({"ok": False, "error": f"Unknown action: {action}"}), 400

    result = ssh_manager.run_hmc_command(
        host=hmc["host"],
        port=int(hmc.get("ssh_port", 22)),
        username=hmc.get("username", "hscroot"),
        key_path=hmc.get("key_path") or None,
        command=cmd,
    )

    if result.get("ok"):
        return jsonify({
            "ok":      True,
            "message": result.get("output", "").strip() or "Action submitted.",
            "command": cmd,
        })
    return jsonify({
        "ok":      False,
        "error":   (result.get("error") or result.get("stderr")
                    or "chsysstate failed").strip(),
        "command": cmd,
    }), 400



# ──────────────────────────────────────────────────────────
# SAN switch management API (read-only for now)
# ──────────────────────────────────────────────────────────

@app.route("/api/san/switches", methods=["GET"])
def list_san_switches():
    """Return all saved SAN switches (secrets stripped)."""
    return jsonify(san_store.list())


@app.route("/api/san/switches", methods=["POST"])
def add_san_switch():
    """Save a new SAN switch entry."""
    data = request.get_json(force=True) or {}
    required = ["name", "host"]
    missing = [k for k in required if not data.get(k)]
    if missing:
        return jsonify({"error": f"Missing fields: {', '.join(missing)}"}), 400
    entry = san_store.add(data)
    return jsonify(entry), 201


@app.route("/api/san/switches/<switch_id>", methods=["DELETE"])
def delete_san_switch(switch_id):
    san_store.remove(switch_id)
    return jsonify({"ok": True})


@app.route("/api/san/switches/<switch_id>/test", methods=["POST"])
def test_san_switch(switch_id):
    """Test SSH connectivity to a SAN switch."""
    sw = san_store.get(switch_id)
    if not sw:
        return jsonify({"error": "Switch not found"}), 404
    result = ssh_manager.test_connection(
        host=sw["host"],
        port=int(sw.get("ssh_port", 22)),
        username=sw.get("username", "admin"),
        key_path=sw.get("key_path") or None,
        password=sw.get("password") or None,
    )
    return jsonify(result)


@app.route("/api/san/switches/<switch_id>/zoning", methods=["GET"])
def get_san_zoning(switch_id):
    """Return the read-only fabric view: aliases, zones, zonesets, active cfg."""
    sw = san_store.get(switch_id)
    if not sw:
        return jsonify({"ok": False, "error": "Switch not found"}), 404
    debug = request.args.get("debug") == "1"
    result = san_manager.fetch_zoning(sw, debug=debug)
    return jsonify(result)


@app.route("/api/san/switches/<switch_id>/ports", methods=["GET"])
def get_san_ports(switch_id):
    """Return the switch port status list (index, state, speed, wwn, type)."""
    sw = san_store.get(switch_id)
    if not sw:
        return jsonify({"ok": False, "error": "Switch not found"}), 404
    debug = request.args.get("debug") == "1"
    result = san_manager.fetch_ports(sw, debug=debug)
    return jsonify(result)


@app.route("/api/san/switches/<switch_id>/ports/<int:port_index>/login", methods=["GET"])
def get_port_login(switch_id, port_index):
    """Run portloginshow <port> and return connected WWPNs."""
    sw = san_store.get(switch_id)
    if not sw:
        return jsonify({"ok": False, "error": "Switch not found"}), 404
    result = san_manager.fetch_port_login(sw, port_index)
    return jsonify(result)


@app.route("/api/hmcs/<hmc_id>/managed-systems/<path:system_id>/lpars-list", methods=["GET"])
def lpars_list_for_vfc(hmc_id, system_id):
    """Return a minimal list of LPAR names for a managed system (for VFC map dropdowns)."""
    hmc = hmc_store.get(hmc_id)
    if not hmc:
        return jsonify({"ok": False, "error": "HMC not found"}), 404
    cmd = f'lssyscfg -r lpar -m "{system_id}" -F name:lpar_id:lpar_env'
    result = ssh_manager.run_hmc_command(
        host=hmc["host"], port=int(hmc.get("ssh_port", 22)),
        username=hmc.get("username", "hscroot"),
        key_path=hmc.get("key_path") or None,
        command=cmd,
    )
    if not result.get("ok"):
        return jsonify({"ok": False, "error": result.get("error", "lssyscfg failed")}), 400
    lpars = []
    for line in result.get("output", "").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(":")
        lpars.append({
            "name":    parts[0],
            "lpar_id": parts[1] if len(parts) > 1 else "",
            "env":     parts[2] if len(parts) > 2 else "",
        })
    return jsonify({"ok": True, "lpars": lpars})


@app.route("/api/hmcs/<hmc_id>/vfc-map/profile", methods=["POST"])
def create_vfc_map_profile(hmc_id):
    """Add VFC adapter to LPAR profile (for stopped LPARs).

    Body: {managed_system, vios_name, vios_slot, client_lpar, profile_name,
           client_slot, wwpn1 (optional), wwpn2 (optional)}
    Step 1: chhwres on VIOS (server side)
    Step 2: chsyscfg -r prof (update LPAR profile)
    """
    import random
    import time as _time

    hmc = hmc_store.get(hmc_id)
    if not hmc:
        return jsonify({"ok": False, "error": "HMC not found"}), 404
    data = request.get_json(force=True) or {}
    managed_system = (data.get("managed_system") or "").strip()
    vios_name      = (data.get("vios_name") or "").strip()
    vios_slot      = str(data.get("vios_slot") or "").strip()
    client_lpar    = (data.get("client_lpar") or "").strip()
    profile_name   = (data.get("profile_name") or "").strip()
    client_slot    = str(data.get("client_slot") or "").strip()
    wwpn1          = (data.get("wwpn1") or "").strip().replace(":", "").lower()
    wwpn2          = (data.get("wwpn2") or "").strip().replace(":", "").lower()

    missing = [k for k, v in [
        ("managed_system", managed_system), ("vios_name", vios_name),
        ("vios_slot", vios_slot), ("client_lpar", client_lpar),
        ("profile_name", profile_name), ("client_slot", client_slot)
    ] if not v]
    if missing:
        return jsonify({"ok": False, "error": f"Missing: {', '.join(missing)}"}), 400

    def gen_wwpn():
        import time as _t; _t.sleep(0.001)
        b = [random.randint(0, 255) for _ in range(6)]
        return "1000" + "".join(f"{x:02x}" for x in b)

    if not wwpn1: wwpn1 = gen_wwpn()
    if not wwpn2: wwpn2 = gen_wwpn()
    while wwpn2 == wwpn1: wwpn2 = gen_wwpn()

    # Use plain hex (no colons) in the command line
    wwpn1_fmt = wwpn1.lower()
    wwpn2_fmt = wwpn2.lower()

    # ── Step 1: VIOS server side (chhwres) ────────────────────────
    cmd1 = (
        f'chhwres -r virtualio -m "{managed_system}" -o a '
        f'-p "{vios_name}" --rsubtype fc -s {vios_slot} '
        f'-a "adapter_type=server,remote_lpar_name={client_lpar},remote_slot_num={client_slot}"'
    )
    r1 = ssh_manager.run_hmc_command(
        host=hmc["host"], port=int(hmc.get("ssh_port", 22)),
        username=hmc.get("username", "hscroot"),
        key_path=hmc.get("key_path") or None,
        command=cmd1,
    )
    ok1  = r1.get("ok", False)
    out1 = (r1.get("output") or "").strip()
    err1 = (r1.get("error") or r1.get("stderr") or "").strip()
    if not ok1:
        return jsonify({
            "ok": False, "error": f"Step 1 failed: {err1 or out1 or 'chhwres failed'}",
            "steps": [{"step": 1, "label": "VIOS server side (chhwres)",
                        "command": cmd1, "output": out1, "error": err1, "ok": False}],
        }), 400

    # ── Step 2: Update LPAR profile (chsyscfg -r prof) ────────────
    # Correct HMC chsyscfg syntax:
    #   chsyscfg -r prof -m "SYS" -i "lpar_name=X,name=P,\"virtual_fc_adapters+=\"\"spec\"\"\""
    # adapter spec: slot/client//remote_lpar_name/remote_slot/wwpn1,wwpn2/is_required
    adapter_spec = f'{client_slot}/client//{vios_name}/{vios_slot}/{wwpn1_fmt},{wwpn2_fmt}/0'
    cmd2 = (
        f'chsyscfg -r prof -m "{managed_system}" --force '
        f'-i "lpar_name={client_lpar},name={profile_name},'
        f'\\"virtual_fc_adapters+=\\"\\"{adapter_spec}\\"\\"\\""'
    )
    r2 = ssh_manager.run_hmc_command(
        host=hmc["host"], port=int(hmc.get("ssh_port", 22)),
        username=hmc.get("username", "hscroot"),
        key_path=hmc.get("key_path") or None,
        command=cmd2,
    )
    ok2  = r2.get("ok", False)
    out2 = (r2.get("output") or "").strip()
    err2 = (r2.get("error") or r2.get("stderr") or "").strip()

    steps = [
        {"step": 1, "label": "VIOS server side (chhwres)", "command": cmd1,
         "output": out1 or "(no output — success)", "error": "", "ok": True},
        {"step": 2, "label": f"Update LPAR profile '{profile_name}' (chsyscfg)", "command": cmd2,
         "output": out2 or "(no output — success)" if ok2 else out2,
         "error": err2 if not ok2 else "", "ok": ok2},
    ]
    if not ok2:
        return jsonify({
            "ok": False, "error": f"Step 2 failed: {err2 or out2 or 'chsyscfg failed'}",
            "wwpn1": wwpn1_fmt, "wwpn2": wwpn2_fmt, "steps": steps,
        }), 400

    return jsonify({
        "ok": True, "message": "VFC added to VIOS (chhwres) and LPAR profile (chsyscfg).",
        "wwpn1": wwpn1_fmt, "wwpn2": wwpn2_fmt,
        "generated_wwpns": not bool((data.get("wwpn1") or "").strip()),
        "steps": steps,
    })


@app.route("/api/hmcs/<hmc_id>/vfc-map", methods=["POST"])
def create_vfc_map(hmc_id):
    """Create a full VFC mapping — Step 1 (VIOS server side) then Step 2 (client side).

    Body: {managed_system, vios_name, vios_slot, client_lpar, client_slot,
           wwpn1 (optional), wwpn2 (optional)}
    If wwpn1/wwpn2 are omitted, two random WWPNs are generated.
    """
    import random
    import time as _time

    hmc = hmc_store.get(hmc_id)
    if not hmc:
        return jsonify({"ok": False, "error": "HMC not found"}), 404
    data = request.get_json(force=True) or {}
    managed_system = (data.get("managed_system") or "").strip()
    vios_name      = (data.get("vios_name") or "").strip()
    vios_slot      = str(data.get("vios_slot") or "").strip()
    client_lpar    = (data.get("client_lpar") or "").strip()
    client_slot    = str(data.get("client_slot") or "").strip()
    wwpn1          = (data.get("wwpn1") or "").strip().replace(":", "").lower()
    wwpn2          = (data.get("wwpn2") or "").strip().replace(":", "").lower()

    missing = [k for k, v in [
        ("managed_system", managed_system), ("vios_name", vios_name),
        ("vios_slot", vios_slot), ("client_lpar", client_lpar), ("client_slot", client_slot)
    ] if not v]
    if missing:
        return jsonify({"ok": False, "error": f"Missing: {', '.join(missing)}"}), 400

    # Generate WWPNs if not provided — use 10:00 prefix + random 6 bytes
    def gen_wwpn():
        seed = int(_time.time() * 1000) ^ random.randint(0, 0xFFFFFFFF)
        rand = random.Random(seed)
        b = [rand.randint(0, 255) for _ in range(6)]
        return "1000" + "".join(f"{x:02x}" for x in b)

    if not wwpn1:
        wwpn1 = gen_wwpn()
    if not wwpn2:
        import time as _t2; _t2.sleep(0.001)
        wwpn2 = gen_wwpn()
    # Ensure the two WWPNs differ
    while wwpn2 == wwpn1:
        wwpn2 = gen_wwpn()

    # Use plain hex (no colons) in the command line
    wwpn1_fmt = wwpn1.lower()
    wwpn2_fmt = wwpn2.lower()

    # ── Step 1: VIOS server side ──────────────────────────────────
    cmd1 = (
        f'chhwres -r virtualio -m "{managed_system}" -o a '
        f'-p "{vios_name}" --rsubtype fc -s {vios_slot} '
        f'-a "adapter_type=server,remote_lpar_name={client_lpar},remote_slot_num={client_slot}"'
    )
    r1 = ssh_manager.run_hmc_command(
        host=hmc["host"], port=int(hmc.get("ssh_port", 22)),
        username=hmc.get("username", "hscroot"),
        key_path=hmc.get("key_path") or None,
        command=cmd1,
    )
    ok1  = r1.get("ok", False)
    out1 = (r1.get("output") or "").strip()
    err1 = (r1.get("error") or r1.get("stderr") or "").strip()

    if not ok1:
        return jsonify({
            "ok": False,
            "error": f"Step 1 failed: {err1 or out1 or 'chhwres failed'}",
            "steps": [{"step": 1, "label": "VIOS server side", "command": cmd1,
                        "output": out1, "error": err1, "ok": False}],
        }), 400

    # ── Step 2: Client LPAR side ──────────────────────────────────
    cmd2 = (
        f'chhwres -r virtualio -m "{managed_system}" -o a '
        f'-p "{client_lpar}" --rsubtype fc -s {client_slot} '
        f'-a "adapter_type=client,remote_lpar_name={vios_name},'
        f'remote_slot_num={vios_slot},wwpns={wwpn1_fmt},{wwpn2_fmt}"'
    )
    r2 = ssh_manager.run_hmc_command(
        host=hmc["host"], port=int(hmc.get("ssh_port", 22)),
        username=hmc.get("username", "hscroot"),
        key_path=hmc.get("key_path") or None,
        command=cmd2,
    )
    ok2  = r2.get("ok", False)
    out2 = (r2.get("output") or "").strip()
    err2 = (r2.get("error") or r2.get("stderr") or "").strip()

    steps = [
        {"step": 1, "label": "VIOS server side",  "command": cmd1,
         "output": out1 or "(no output — success)", "error": "", "ok": True},
        {"step": 2, "label": "Client LPAR side",  "command": cmd2,
         "output": out2 or "(no output — success)" if ok2 else out2,
         "error": err2 if not ok2 else "", "ok": ok2},
    ]

    if not ok2:
        return jsonify({
            "ok": False,
            "error": f"Step 2 failed: {err2 or out2 or 'chhwres failed'}",
            "wwpn1": wwpn1_fmt, "wwpn2": wwpn2_fmt,
            "steps": steps,
        }), 400

    return jsonify({
        "ok": True,
        "message": "VFC mapping created (both sides).",
        "wwpn1": wwpn1_fmt,
        "wwpn2": wwpn2_fmt,
        "generated_wwpns": not bool((data.get("wwpn1") or "").strip()),
        "steps": steps,
    })


@app.route("/api/hmcs/<hmc_id>/vfc-map/vfchost-list", methods=["GET"])
def list_vfchost_for_client(hmc_id):
    """Return the list of vfchostX adapters on a VIOS that are mapped to a specific client LPAR.

    Query params: managed_system, vios_name, client_lpar_name
    Steps:
      1. lssyscfg to get numeric lpar_id for client_lpar_name
      2. viosvrcmd lsmap -all -npiv -cpid <lpar_id> -fmt ,
         → first column is the vfchostX name
    """
    import re as _re

    hmc = hmc_store.get(hmc_id)
    if not hmc:
        return jsonify({"ok": False, "error": "HMC not found"}), 404

    managed_system    = (request.args.get("managed_system") or "").strip()
    vios_name         = (request.args.get("vios_name") or "").strip()
    client_lpar_name  = (request.args.get("client_lpar_name") or "").strip()

    if not managed_system or not vios_name or not client_lpar_name:
        return jsonify({"ok": False,
                        "error": "managed_system, vios_name and client_lpar_name are required"}), 400

    # ── Step 1: get numeric lpar_id ───────────────────────────────────────
    cmd_lssyscfg = (
        f'lssyscfg -m "{managed_system}" -r lpar '
        f'--filter "lpar_names={client_lpar_name}" -F lpar_id'
    )
    r1 = ssh_manager.run_hmc_command(
        host=hmc["host"], port=int(hmc.get("ssh_port", 22)),
        username=hmc.get("username", "hscroot"),
        key_path=hmc.get("key_path") or None,
        command=cmd_lssyscfg,
    )
    if not r1.get("ok"):
        return jsonify({
            "ok": False,
            "error": (r1.get("error") or r1.get("stderr") or "lssyscfg failed").strip(),
            "command": cmd_lssyscfg,
        }), 400

    lpar_id = ""
    for line in (r1.get("output") or "").splitlines():
        line = line.strip()
        if line and line.isdigit():
            lpar_id = line
            break
        elif line and not line.lower().startswith("error") and not line.startswith("No results"):
            lpar_id = line
            break

    if not lpar_id:
        return jsonify({
            "ok": False,
            "error": f"Could not determine lpar_id for '{client_lpar_name}'",
            "command": cmd_lssyscfg,
        }), 400

    # ── Step 2: lsmap -all -npiv -cpid <lpar_id> ─────────────────────────
    cmd_lsmap = (
        f'viosvrcmd -m {managed_system} -p "{vios_name}" '
        f'-c "lsmap -all -npiv -cpid {lpar_id} -fmt ,"'
    )
    r2 = ssh_manager.run_hmc_command(
        host=hmc["host"], port=int(hmc.get("ssh_port", 22)),
        username=hmc.get("username", "hscroot"),
        key_path=hmc.get("key_path") or None,
        command=cmd_lsmap,
    )
    if not r2.get("ok"):
        return jsonify({
            "ok": False,
            "error": (r2.get("error") or r2.get("stderr") or "lsmap failed").strip(),
            "command": cmd_lsmap,
            "lpar_id": lpar_id,
        }), 400

    # Parse: first column of each comma-separated line that looks like vfchostX
    vfchosts = []
    for line in (r2.get("output") or "").splitlines():
        line = line.strip()
        if not line or line.lower().startswith("error") or line.startswith("No results"):
            continue
        col = line.split(",")[0].strip()
        if _re.match(r'^vfchost\d+$', col, _re.IGNORECASE) and col not in vfchosts:
            vfchosts.append(col)

    return jsonify({
        "ok": True,
        "vfchosts": vfchosts,
        "lpar_id": lpar_id,
        "commands": [cmd_lssyscfg, cmd_lsmap],
    })


@app.route("/api/hmcs/<hmc_id>/vfc-map/fcs-adapters", methods=["GET"])
def list_fcs_adapters(hmc_id):
    """Return a list of physical FC adapters (fcsX) on a given VIOS.

    Query params: managed_system, vios_name
    Runs: viosvrcmd -m <managed_system> -p <vios_name> -c "lsdev"
    Returns lines whose first column matches fcsX.
    """
    import re as _re

    hmc = hmc_store.get(hmc_id)
    if not hmc:
        return jsonify({"ok": False, "error": "HMC not found"}), 404

    managed_system = (request.args.get("managed_system") or "").strip()
    vios_name      = (request.args.get("vios_name") or "").strip()

    if not managed_system or not vios_name:
        return jsonify({"ok": False, "error": "managed_system and vios_name are required"}), 400

    cmd = f'viosvrcmd -m {managed_system} -p "{vios_name}" -c "lsdev"'
    result = ssh_manager.run_hmc_command(
        host=hmc["host"], port=int(hmc.get("ssh_port", 22)),
        username=hmc.get("username", "hscroot"),
        key_path=hmc.get("key_path") or None,
        command=cmd,
    )
    ok  = result.get("ok", False)
    out = (result.get("output") or "").strip()
    err = (result.get("error") or result.get("stderr") or "").strip()

    if not ok:
        return jsonify({"ok": False, "error": err or "lsdev failed", "command": cmd}), 400

    adapters = []
    for line in out.splitlines():
        if "fcs" not in line.lower():
            continue
        parts = line.split()
        if not parts:
            continue
        name = parts[0].strip()
        if _re.match(r'^fcs\d+$', name, _re.IGNORECASE):
            # Grab the rest of the line as description
            desc = " ".join(parts[1:]).strip() if len(parts) > 1 else ""
            adapters.append({"name": name, "description": desc})

    return jsonify({"ok": True, "adapters": adapters, "command": cmd})


@app.route("/api/hmcs/<hmc_id>/vfc-map/vfcmap", methods=["POST"])
def do_vfcmap(hmc_id):
    """Map a vfchost virtual adapter to a physical FC port on the VIOS.

    Step A: viosvrcmd lsdev | grep fcs  → find physical FC adapters (fcsX)
    Step B: viosvrcmd lsmap -all -npiv  → match clntid to client LPAR id, get vfchostX
    Step C: viosvrcmd vfcmap -vadapter <vfchostX> -fcp <fcsX>

    Body: {managed_system, vios_name, client_lpar_id}
      client_lpar_id: numeric partition id of the client LPAR (used to match lsmap clntid)
    """
    import re as _re

    hmc = hmc_store.get(hmc_id)
    if not hmc:
        return jsonify({"ok": False, "error": "HMC not found"}), 404
    data = request.get_json(force=True) or {}
    managed_system   = (data.get("managed_system") or "").strip()
    vios_name        = (data.get("vios_name") or "").strip()
    # vfchost_name may be supplied directly by the user from the dropdown;
    # if omitted, auto-detect via lsmap using client_lpar_id (Step B).
    vfchost_name     = (data.get("vfchost_name") or "").strip()
    client_lpar_id   = str(data.get("client_lpar_id") or "").strip()
    # physical_adapter may be supplied by the user from the dropdown;
    # if omitted, auto-detect via lsdev (Step A).
    physical_adapter = (data.get("physical_adapter") or "").strip()

    # Require either vfchost_name or client_lpar_id for Step B
    missing = [k for k, v in [
        ("managed_system", managed_system),
        ("vios_name", vios_name),
    ] if not v]
    if not vfchost_name and not client_lpar_id:
        missing.append("vfchost_name or client_lpar_id")
    if missing:
        return jsonify({"ok": False, "error": f"Missing: {', '.join(missing)}"}), 400

    steps = []

    # ── Step A: lsdev on VIOS to find physical FC adapters ────────────────
    # Skipped if the user already selected a physical adapter from the dropdown.
    if physical_adapter:
        # User chose adapter — record as a skipped/informational step
        steps.append({
            "step": "A", "label": "Physical FC adapter (user selected)",
            "command": f"# user selected: {physical_adapter}",
            "output": f"Using user-selected adapter: {physical_adapter}",
            "note": f"Using physical adapter: {physical_adapter}",
            "error": "", "ok": True,
        })
    else:
        cmd_lsdev = f'viosvrcmd -m {managed_system} -p "{vios_name}" -c "lsdev"'
        r_lsdev = ssh_manager.run_hmc_command(
            host=hmc["host"], port=int(hmc.get("ssh_port", 22)),
            username=hmc.get("username", "hscroot"),
            key_path=hmc.get("key_path") or None,
            command=cmd_lsdev,
        )
        ok_lsdev  = r_lsdev.get("ok", False)
        out_lsdev = (r_lsdev.get("output") or "").strip()
        err_lsdev = (r_lsdev.get("error") or r_lsdev.get("stderr") or "").strip()

        steps.append({
            "step": "A", "label": "List physical FC adapters (lsdev | grep fcs)",
            "command": cmd_lsdev + " | grep fcs",
            "output": out_lsdev, "error": err_lsdev if not ok_lsdev else "", "ok": ok_lsdev,
        })

        if not ok_lsdev:
            return jsonify({
                "ok": False,
                "error": f"Step A failed: {err_lsdev or out_lsdev or 'lsdev failed'}",
                "steps": steps,
            }), 400

        # Parse: first column of lines matching "fcs"
        for line in out_lsdev.splitlines():
            if "fcs" in line.lower():
                col = line.split()[0].strip()
                if _re.match(r'^fcs\d+$', col, _re.IGNORECASE):
                    physical_adapter = col
                    break

        if not physical_adapter:
            steps[-1]["error"] = "No fcsX adapter found in lsdev output"
            return jsonify({
                "ok": False,
                "error": "No physical FC adapter (fcsX) found on VIOS via lsdev.",
                "steps": steps,
            }), 400

        steps[-1]["note"] = f"Using physical adapter: {physical_adapter}"

    # ── Step B: lsmap -all -npiv to find vfchostX matching client LPAR id ─
    # Skipped if the user already selected a vfchost from the dropdown.
    if vfchost_name:
        vhost_name = vfchost_name
        steps.append({
            "step": "B", "label": "vfchost adapter (user selected)",
            "command": f"# user selected: {vfchost_name}",
            "output": f"Using user-selected vfchost: {vfchost_name}",
            "note": f"vfchost: {vfchost_name}",
            "error": "", "ok": True,
        })
    else:
        cmd_lsmap = f'viosvrcmd -m {managed_system} -p "{vios_name}" -c "lsmap -all -npiv -fmt ,"'
        r_lsmap = ssh_manager.run_hmc_command(
            host=hmc["host"], port=int(hmc.get("ssh_port", 22)),
            username=hmc.get("username", "hscroot"),
            key_path=hmc.get("key_path") or None,
            command=cmd_lsmap,
        )
        ok_lsmap  = r_lsmap.get("ok", False)
        out_lsmap = (r_lsmap.get("output") or "").strip()
        err_lsmap = (r_lsmap.get("error") or r_lsmap.get("stderr") or "").strip()

        steps.append({
            "step": "B", "label": "List NPIV mappings (lsmap -all -npiv -fmt ,)",
            "command": cmd_lsmap,
            "output": out_lsmap, "error": err_lsmap if not ok_lsmap else "", "ok": ok_lsmap,
        })

        if not ok_lsmap:
            return jsonify({
                "ok": False,
                "error": f"Step B failed: {err_lsmap or out_lsmap or 'lsmap failed'}",
                "steps": steps,
            }), 400

        # lsmap -fmt , columns: vfchostX, physloc, clntid, clntname, clntos, status, ...
        vhost_name = None
        for line in out_lsmap.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split(",")
            if len(parts) < 3:
                continue
            vfchost_col = parts[0].strip()
            clntid_col  = parts[2].strip()
            if clntid_col == client_lpar_id and _re.match(r'^vfchost\d+$', vfchost_col, _re.IGNORECASE):
                vhost_name = vfchost_col
                break

        if not vhost_name:
            steps[-1]["error"] = (
                f"No vfchostX found in lsmap output matching clntid={client_lpar_id}"
            )
            return jsonify({
                "ok": False,
                "error": (
                    f"Could not find vfchostX for client LPAR id {client_lpar_id} "
                    f"in lsmap -all -npiv output."
                ),
                "steps": steps,
            }), 400

        steps[-1]["note"] = f"Found vhost: {vhost_name}"

    # ── Step C: vfcmap -vadapter <vhost> -fcp <fcs> ──────────────────────
    cmd_vfcmap = (
        f'viosvrcmd -m {managed_system} -p "{vios_name}" '
        f'-c "vfcmap -vadapter {vhost_name} -fcp {physical_adapter}"'
    )
    r_vfcmap = ssh_manager.run_hmc_command(
        host=hmc["host"], port=int(hmc.get("ssh_port", 22)),
        username=hmc.get("username", "hscroot"),
        key_path=hmc.get("key_path") or None,
        command=cmd_vfcmap,
    )
    ok_vfcmap  = r_vfcmap.get("ok", False)
    out_vfcmap = (r_vfcmap.get("output") or "").strip()
    err_vfcmap = (r_vfcmap.get("error") or r_vfcmap.get("stderr") or "").strip()

    steps.append({
        "step": "C", "label": f"Map vfchost to physical FC port (vfcmap)",
        "command": cmd_vfcmap,
        "output": out_vfcmap or "(no output — success)" if ok_vfcmap else out_vfcmap,
        "error": err_vfcmap if not ok_vfcmap else "", "ok": ok_vfcmap,
    })

    if not ok_vfcmap:
        return jsonify({
            "ok": False,
            "error": f"Step C failed: {err_vfcmap or out_vfcmap or 'vfcmap failed'}",
            "vhost": vhost_name, "fcs": physical_adapter, "steps": steps,
        }), 400

    return jsonify({
        "ok": True,
        "message": f"vfcmap: {vhost_name} mapped to {physical_adapter} on VIOS {vios_name}.",
        "vhost": vhost_name,
        "fcs": physical_adapter,
        "steps": steps,
    })


@app.route("/api/hmcs/<hmc_id>/vfc-map/vfcmap-remove", methods=["POST"])
def do_vfcmap_remove(hmc_id):
    """Remove the vfchost → physical FC port mapping on the VIOS (vfcmap -remove).

    Step A: viosvrcmd lsmap -all -npiv → match clntid to client LPAR id, get vfchostX
    Step B: viosvrcmd vfcmap -vadapter <vfchostX> -remove

    Body: {managed_system, vios_name, client_lpar_id}
    """
    import re as _re

    hmc = hmc_store.get(hmc_id)
    if not hmc:
        return jsonify({"ok": False, "error": "HMC not found"}), 404
    data = request.get_json(force=True) or {}
    managed_system = (data.get("managed_system") or "").strip()
    vios_name      = (data.get("vios_name") or "").strip()
    client_lpar_id = str(data.get("client_lpar_id") or "").strip()

    missing = [k for k, v in [
        ("managed_system", managed_system),
        ("vios_name", vios_name),
        ("client_lpar_id", client_lpar_id),
    ] if not v]
    if missing:
        return jsonify({"ok": False, "error": f"Missing: {', '.join(missing)}"}), 400

    steps = []

    # ── Step A: lsmap -all -npiv to find vfchostX matching client LPAR id ─
    cmd_lsmap = f'viosvrcmd -m {managed_system} -p "{vios_name}" -c "lsmap -all -npiv -fmt ,"'
    r_lsmap = ssh_manager.run_hmc_command(
        host=hmc["host"], port=int(hmc.get("ssh_port", 22)),
        username=hmc.get("username", "hscroot"),
        key_path=hmc.get("key_path") or None,
        command=cmd_lsmap,
    )
    ok_lsmap  = r_lsmap.get("ok", False)
    out_lsmap = (r_lsmap.get("output") or "").strip()
    err_lsmap = (r_lsmap.get("error") or r_lsmap.get("stderr") or "").strip()

    steps.append({
        "step": "A", "label": "List NPIV mappings (lsmap -all -npiv -fmt ,)",
        "command": cmd_lsmap,
        "output": out_lsmap, "error": err_lsmap if not ok_lsmap else "", "ok": ok_lsmap,
    })

    if not ok_lsmap:
        return jsonify({
            "ok": False,
            "error": f"Step A failed: {err_lsmap or out_lsmap or 'lsmap failed'}",
            "steps": steps,
        }), 400

    # Parse: find vfchostX whose clntid matches client_lpar_id
    vhost_name = None
    for line in out_lsmap.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(",")
        if len(parts) < 3:
            continue
        vfchost_col = parts[0].strip()
        clntid_col  = parts[2].strip()
        if clntid_col == client_lpar_id and _re.match(r'^vfchost\d+$', vfchost_col, _re.IGNORECASE):
            vhost_name = vfchost_col
            break

    if not vhost_name:
        steps[-1]["error"] = f"No vfchostX found in lsmap output matching clntid={client_lpar_id}"
        return jsonify({
            "ok": False,
            "error": f"Could not find vfchostX for client LPAR id {client_lpar_id} in lsmap output.",
            "steps": steps,
        }), 400

    steps[-1]["note"] = f"Found vhost: {vhost_name}"

    # ── Step B: vfcmap -vadapter <vhost> -remove ──────────────────────────
    cmd_remove = (
        f'viosvrcmd -m {managed_system} -p "{vios_name}" '
        f'-c "vfcmap -vadapter {vhost_name} -remove"'
    )
    r_remove = ssh_manager.run_hmc_command(
        host=hmc["host"], port=int(hmc.get("ssh_port", 22)),
        username=hmc.get("username", "hscroot"),
        key_path=hmc.get("key_path") or None,
        command=cmd_remove,
    )
    ok_remove  = r_remove.get("ok", False)
    out_remove = (r_remove.get("output") or "").strip()
    err_remove = (r_remove.get("error") or r_remove.get("stderr") or "").strip()

    steps.append({
        "step": "B", "label": f"Remove vfchost mapping (vfcmap -remove)",
        "command": cmd_remove,
        "output": out_remove or "(no output — success)" if ok_remove else out_remove,
        "error": err_remove if not ok_remove else "", "ok": ok_remove,
    })

    if not ok_remove:
        return jsonify({
            "ok": False,
            "error": f"Step B failed: {err_remove or out_remove or 'vfcmap -remove failed'}",
            "vhost": vhost_name, "steps": steps,
        }), 400

    return jsonify({
        "ok": True,
        "message": f"VFC mapping removed: {vhost_name} on VIOS {vios_name}.",
        "vhost": vhost_name,
        "steps": steps,
    })


@app.route("/api/hmcs/<hmc_id>/vfc-map/delete", methods=["POST"])
def delete_vfc_map(hmc_id):
    """Delete a VFC mapping: chhwres -o r (remove server-side vfchost adapter).
    Body: {managed_system, vios_name, vios_slot}
    """
    hmc = hmc_store.get(hmc_id)
    if not hmc:
        return jsonify({"ok": False, "error": "HMC not found"}), 404
    data = request.get_json(force=True) or {}
    managed_system = (data.get("managed_system") or "").strip()
    vios_name      = (data.get("vios_name") or "").strip()
    vios_slot      = str(data.get("vios_slot") or "").strip()

    missing = [k for k, v in [
        ("managed_system", managed_system), ("vios_name", vios_name), ("vios_slot", vios_slot)
    ] if not v]
    if missing:
        return jsonify({"ok": False, "error": f"Missing: {', '.join(missing)}"}), 400

    cmd = (
        f'chhwres -r virtualio -m "{managed_system}" -o r '
        f'-p "{vios_name}" --rsubtype fc -s {vios_slot}'
    )
    result = ssh_manager.run_hmc_command(
        host=hmc["host"], port=int(hmc.get("ssh_port", 22)),
        username=hmc.get("username", "hscroot"),
        key_path=hmc.get("key_path") or None,
        command=cmd,
    )
    ok  = result.get("ok", False)
    out = (result.get("output") or "").strip()
    err = (result.get("error") or result.get("stderr") or "").strip()
    if ok:
        return jsonify({"ok": True, "message": out or "VFC mapping removed successfully.", "command": cmd})
    return jsonify({"ok": False, "error": err or out or "chhwres failed", "command": cmd}), 400


@app.route("/api/hmcs/<hmc_id>/vfc-adapter/create", methods=["POST"])
def create_vfc_adapter(hmc_id):
    """Create a virtual FC adapter on an LPAR (client side): chhwres -o a --rsubtype fc.
    Body: {managed_system, lpar_name, slot_num, adapter_type (client|server)}
    """
    hmc = hmc_store.get(hmc_id)
    if not hmc:
        return jsonify({"ok": False, "error": "HMC not found"}), 404
    data = request.get_json(force=True) or {}
    managed_system = (data.get("managed_system") or "").strip()
    lpar_name      = (data.get("lpar_name") or "").strip()
    slot_num       = str(data.get("slot_num") or "").strip()
    adapter_type   = (data.get("adapter_type") or "client").strip()

    missing = [k for k, v in [
        ("managed_system", managed_system), ("lpar_name", lpar_name), ("slot_num", slot_num)
    ] if not v]
    if missing:
        return jsonify({"ok": False, "error": f"Missing: {', '.join(missing)}"}), 400

    cmd = (
        f'chhwres -r virtualio -m "{managed_system}" -o a '
        f'-p "{lpar_name}" --rsubtype fc -s {slot_num} '
        f'-a "adapter_type={adapter_type}"'
    )
    result = ssh_manager.run_hmc_command(
        host=hmc["host"], port=int(hmc.get("ssh_port", 22)),
        username=hmc.get("username", "hscroot"),
        key_path=hmc.get("key_path") or None,
        command=cmd,
    )
    ok  = result.get("ok", False)
    out = (result.get("output") or "").strip()
    err = (result.get("error") or result.get("stderr") or "").strip()
    if ok:
        return jsonify({"ok": True, "message": out or "Virtual FC adapter created.", "command": cmd})
    return jsonify({"ok": False, "error": err or out or "chhwres failed", "command": cmd}), 400


@app.route("/api/hmcs/<hmc_id>/vfc-adapter/delete", methods=["POST"])
def delete_vfc_adapter(hmc_id):
    """Delete a virtual FC adapter from an LPAR: chhwres -o r --rsubtype fc.
    Body: {managed_system, lpar_name, slot_num}
    """
    hmc = hmc_store.get(hmc_id)
    if not hmc:
        return jsonify({"ok": False, "error": "HMC not found"}), 404
    data = request.get_json(force=True) or {}
    managed_system = (data.get("managed_system") or "").strip()
    lpar_name      = (data.get("lpar_name") or "").strip()
    slot_num       = str(data.get("slot_num") or "").strip()

    missing = [k for k, v in [
        ("managed_system", managed_system), ("lpar_name", lpar_name), ("slot_num", slot_num)
    ] if not v]
    if missing:
        return jsonify({"ok": False, "error": f"Missing: {', '.join(missing)}"}), 400

    cmd = (
        f'chhwres -r virtualio -m "{managed_system}" -o r '
        f'-p "{lpar_name}" --rsubtype fc -s {slot_num}'
    )
    result = ssh_manager.run_hmc_command(
        host=hmc["host"], port=int(hmc.get("ssh_port", 22)),
        username=hmc.get("username", "hscroot"),
        key_path=hmc.get("key_path") or None,
        command=cmd,
    )
    ok  = result.get("ok", False)
    out = (result.get("output") or "").strip()
    err = (result.get("error") or result.get("stderr") or "").strip()
    if ok:
        return jsonify({"ok": True, "message": out or "Virtual FC adapter deleted.", "command": cmd})
    return jsonify({"ok": False, "error": err or out or "chhwres failed", "command": cmd}), 400


@app.route("/api/san/switches/<switch_id>/alias", methods=["POST"])
def create_san_alias(switch_id):
    """Create a new alias on the switch.
    Body: {name: str, members: [wwpn, ...]}
    """
    sw = san_store.get(switch_id)
    if not sw:
        return jsonify({"ok": False, "error": "Switch not found"}), 404
    data = request.get_json(force=True) or {}
    name = (data.get("name") or "").strip()
    members = [m.strip() for m in (data.get("members") or []) if m.strip()]
    if not name:
        return jsonify({"ok": False, "error": "Alias name is required"}), 400
    if not members:
        return jsonify({"ok": False, "error": "At least one WWPN member is required"}), 400
    result = san_manager.create_alias(sw, name, members)
    status = 200 if result.get("ok") else 400
    return jsonify(result), status


@app.route("/api/san/switches/<switch_id>/zone", methods=["POST"])
def create_san_zone(switch_id):
    """Create a new zone on the switch.
    Body: {name: str, members: [alias_or_wwpn, ...]}
    """
    sw = san_store.get(switch_id)
    if not sw:
        return jsonify({"ok": False, "error": "Switch not found"}), 404
    data = request.get_json(force=True) or {}
    name = (data.get("name") or "").strip()
    members = [m.strip() for m in (data.get("members") or []) if m.strip()]
    if not name:
        return jsonify({"ok": False, "error": "Zone name is required"}), 400
    if not members:
        return jsonify({"ok": False, "error": "At least one member is required"}), 400
    result = san_manager.create_zone(sw, name, members)
    status = 200 if result.get("ok") else 400
    return jsonify(result), status


@app.route("/api/san/switches/<switch_id>/zoneset/add-zones", methods=["POST"])
def add_zones_to_zoneset(switch_id):
    """Add zones to an existing or new zoneset.
    Body: {zoneset: str, zones: [zone_name, ...]}
    """
    sw = san_store.get(switch_id)
    if not sw:
        return jsonify({"ok": False, "error": "Switch not found"}), 404
    data = request.get_json(force=True) or {}
    zoneset = (data.get("zoneset") or "").strip()
    zones = [z.strip() for z in (data.get("zones") or []) if z.strip()]
    if not zoneset:
        return jsonify({"ok": False, "error": "Zoneset name is required"}), 400
    if not zones:
        return jsonify({"ok": False, "error": "At least one zone is required"}), 400
    result = san_manager.add_zone_to_zoneset(sw, zoneset, zones)
    status = 200 if result.get("ok") else 400
    return jsonify(result), status


@app.route("/api/san/switches/<switch_id>/alias/<path:alias_name>", methods=["DELETE"])
def delete_san_alias(switch_id, alias_name):
    sw = san_store.get(switch_id)
    if not sw:
        return jsonify({"ok": False, "error": "Switch not found"}), 404
    result = san_manager.delete_alias(sw, alias_name)
    return jsonify(result), (200 if result.get("ok") else 400)


@app.route("/api/san/switches/<switch_id>/zone/<path:zone_name>", methods=["DELETE"])
def delete_san_zone(switch_id, zone_name):
    sw = san_store.get(switch_id)
    if not sw:
        return jsonify({"ok": False, "error": "Switch not found"}), 404
    result = san_manager.delete_zone(sw, zone_name)
    return jsonify(result), (200 if result.get("ok") else 400)


@app.route("/api/san/switches/<switch_id>/zoneset/activate", methods=["POST"])
def activate_san_zoneset(switch_id):
    """Activate (enable) a zoneset / cfg.
    Body: {zoneset: str}
    """
    sw = san_store.get(switch_id)
    if not sw:
        return jsonify({"ok": False, "error": "Switch not found"}), 404
    data = request.get_json(force=True) or {}
    zoneset = (data.get("zoneset") or "").strip()
    if not zoneset:
        return jsonify({"ok": False, "error": "Zoneset name is required"}), 400
    result = san_manager.activate_zoneset(sw, zoneset)
    status = 200 if result.get("ok") else 400
    return jsonify(result), status



# ──────────────────────────────────────────────────────────
# WebSocket terminal (SSH)
# ──────────────────────────────────────────────────────────

@socketio.on("ssh_connect")
def handle_ssh_connect(data):

    hmc_id = data.get("hmc_id")
    hmc = hmc_store.get(hmc_id)
    if not hmc:
        emit("ssh_error", {"msg": "HMC not found"})
        return
    result = ssh_manager.open_shell(
        sid=request.sid,
        host=hmc["host"],
        port=int(hmc.get("ssh_port", 22)),
        username=data.get("username", hmc.get("username", "hscroot")),
        key_path=data.get("key_path") or hmc.get("key_path"),
        password=data.get("password"),
        emit_fn=lambda msg: socketio.emit("ssh_output", {"data": msg},
                                          to=request.sid),
    )
    if not result.get("ok"):
        emit("ssh_error", {"msg": result.get("error", "Connection failed")})


@socketio.on("ssh_input")
def handle_ssh_input(data):
    ssh_manager.send_input(request.sid, data.get("data", ""))


@socketio.on("ssh_disconnect_req")
def handle_ssh_disconnect(data=None):
    ssh_manager.close_shell(request.sid)


@socketio.on("disconnect")
def handle_disconnect():
    ssh_manager.close_shell(request.sid)


# ──────────────────────────────────────────────────────────
# WebSocket LPAR vterm console  (namespace /console)
# ──────────────────────────────────────────────────────────
#
# Flow:
#   1. Browser connects to /console namespace.
#   2. Browser emits "vterm_open" with {hmc_id, system_id, lpar_name}.
#   3. Backend calls SSHManager.open_vterm() which SSHs to the HMC and
#      runs:  mkvterm -m "<system_id>" -p "<lpar_name>"
#   4. A background thread in SSHManager reads the channel and emits
#      "vterm_output" events back to the browser.
#   5. Browser emits "vterm_input" with {data: "<keystrokes>"}.
#   6. On modal close / browser disconnect, "vterm_close" is emitted and
#      the backend tears down the SSH channel (releasing the HMC vterm lock).
# ──────────────────────────────────────────────────────────

@socketio.on("vterm_open", namespace="/console")
def handle_vterm_open(data):
    """Connect to the HMC and start the mkvterm session for the given LPAR."""
    hmc_id     = data.get("hmc_id")
    system_id  = data.get("system_id", "")
    lpar_name  = data.get("lpar_name", "")

    hmc = hmc_store.get(hmc_id)
    if not hmc:
        emit("vterm_error", {"msg": "HMC not found"}, namespace="/console")
        return
    if not system_id or not lpar_name:
        emit("vterm_error", {"msg": "system_id and lpar_name are required"},
             namespace="/console")
        return

    sid = request.sid

    def _emit_output(text):
        """Called from the SSHManager read-thread; forward terminal bytes to browser."""
        socketio.emit("vterm_output", {"data": text}, to=sid, namespace="/console")

    result = ssh_manager.open_vterm(
        sid=sid,
        host=hmc["host"],
        port=int(hmc.get("ssh_port", 22)),
        username=hmc.get("username", "hscroot"),
        key_path=hmc.get("key_path") or None,
        managed_system=system_id,
        lpar_name=lpar_name,
        emit_fn=_emit_output,
    )

    if result.get("ok"):
        logger.info("vterm opened for LPAR %s on %s (sid=%s)", lpar_name, system_id, sid)
        emit("vterm_ready", {"lpar_name": lpar_name, "system_id": system_id},
             namespace="/console")
    else:
        err_msg = result.get("error", "Failed to open vterm")
        logger.warning("vterm open failed for %s: %s", lpar_name, err_msg)
        # Detect the common "vterm already open" error from the HMC.
        if "already" in err_msg.lower() or "locked" in err_msg.lower() or "in use" in err_msg.lower():
            err_msg = (
                f"The vterm for LPAR '{lpar_name}' is already open (or locked) on the HMC. "
                "Close the existing vterm session on the HMC first:\n"
                f"  rmvterm -m \"{system_id}\" -p \"{lpar_name}\"\n"
                "Then try again."
            )
        emit("vterm_error", {"msg": err_msg}, namespace="/console")


@socketio.on("vterm_input", namespace="/console")
def handle_vterm_input(data):
    """Forward keystrokes from the browser terminal to the HMC SSH channel."""
    ssh_manager.send_input(request.sid, data.get("data", ""))


@socketio.on("vterm_resize", namespace="/console")
def handle_vterm_resize(data):
    """Propagate terminal resize (cols x rows) to the PTY on the HMC."""
    try:
        cols = int(data.get("cols", 80))
        rows = int(data.get("rows", 24))
    except (TypeError, ValueError):
        return
    ssh_manager.resize_vterm(request.sid, cols, rows)


@socketio.on("vterm_close", namespace="/console")
def handle_vterm_close(data=None):
    """Explicit close from the browser (modal close button)."""
    ssh_manager.close_shell(request.sid)
    logger.info("vterm closed by client (sid=%s)", request.sid)


@socketio.on("disconnect", namespace="/console")
def handle_vterm_disconnect():
    """Browser disconnected (tab closed, network drop, etc.) — clean up SSH."""
    ssh_manager.close_shell(request.sid)
    logger.info("vterm disconnect cleanup (sid=%s)", request.sid)


# ──────────────────────────────────────────────────────────
# IBM Storage FlashSystem / Storage Virtualize REST API
# ──────────────────────────────────────────────────────────

import requests as _requests
import urllib3 as _urllib3

_urllib3.disable_warnings(_urllib3.exceptions.InsecureRequestWarning)

_STORAGE_API_VERSION = "v1"


def _storage_headers(token: str) -> dict:
    """Return headers for an authenticated Storage Virtualize REST request."""
    return {
        "X-Auth-Token": token,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _storage_base(ip: str) -> str:
    return f"https://{ip}:7443/rest/{_STORAGE_API_VERSION}"


# ── Session helpers ────────────────────────────────────────

@app.route("/api/storage/login", methods=["POST"])
def storage_login():
    """Authenticate to IBM Storage Virtualize.

    Credentials are passed as HTTP headers (X-Auth-Username / X-Auth-Password),
    matching the exact curl pattern that is known to work against this system.
    """
    data = request.get_json(force=True) or {}
    storage_ip = (data.get("storage_ip") or "").strip()
    username   = (data.get("username") or "").strip()
    password   = data.get("password") or ""

    if not storage_ip or not username or not password:
        return jsonify({"ok": False, "error": "storage_ip, username and password are required"}), 400

    url = f"{_storage_base(storage_ip)}/auth"
    try:
        resp = _requests.post(
            url,
            headers={
                "X-Auth-Username": username,
                "X-Auth-Password": password,
                "Content-Type": "application/json",
            },
            verify=False,
            timeout=15,
        )
    except _requests.exceptions.ConnectionError as exc:
        return jsonify({"ok": False, "error": f"Connection failed: {exc}"}), 502
    except _requests.exceptions.Timeout:
        return jsonify({"ok": False, "error": "Connection timed out"}), 504
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500

    if resp.status_code in (200, 201):
        token = resp.headers.get("X-Auth-Token") or (resp.json().get("token") if resp.content else None)
        if not token:
            # Some firmware versions embed the token in the JSON body
            try:
                token = resp.json().get("token")
            except Exception:
                token = None
        if not token:
            return jsonify({"ok": False, "error": "Authenticated but no token received in response"}), 502
        session["storage_ip"]       = storage_ip
        session["storage_token"]    = token
        session["storage_username"] = username
        # Remember this storage system (IP + username only — never the password)
        # so it can be offered for one-click reconnect next time.
        try:
            storage_store.add({"storage_ip": storage_ip, "username": username,
                               "name": storage_ip})
        except Exception:
            pass
        return jsonify({"ok": True, "token": token, "username": username, "storage_ip": storage_ip})


    if resp.status_code in (401, 403):
        return jsonify({"ok": False, "error": "Invalid credentials"}), 401

    return jsonify({"ok": False, "error": f"HTTP {resp.status_code}: {resp.text[:300]}"}), resp.status_code


@app.route("/api/storage/logout", methods=["POST"])
def storage_logout():
    """Delete the active token on the storage array and clear the Flask session."""
    token      = session.get("storage_token")
    storage_ip = session.get("storage_ip")
    if token and storage_ip:
        try:
            _requests.delete(
                f"{_storage_base(storage_ip)}/auth",
                headers=_storage_headers(token),
                verify=False,
                timeout=10,
            )
        except Exception:
            pass
    session.pop("storage_ip",       None)
    session.pop("storage_token",    None)
    session.pop("storage_username", None)
    return jsonify({"ok": True})


@app.route("/api/storage/session", methods=["GET"])
def storage_session_status():
    """Return current storage session info, including the auth token so the UI
    can display it (masked by default, revealable via the eye icon)."""
    if session.get("storage_token"):
        return jsonify({
            "ok": True,
            "connected": True,
            "storage_ip": session.get("storage_ip"),
            "username":   session.get("storage_username"),
            "token":      session.get("storage_token"),
        })
    return jsonify({"ok": True, "connected": False})


# ── Saved storage systems (IP + username only — no password stored) ─────────

@app.route("/api/storage/systems", methods=["GET"])
def storage_list_systems():
    """Return all saved storage systems (IP + username; never any password)."""
    return jsonify({"ok": True, "data": storage_store.list()})


@app.route("/api/storage/systems", methods=["POST"])
def storage_add_system():
    """Manually save a storage system record (IP + username)."""
    data = request.get_json(force=True) or {}
    storage_ip = (data.get("storage_ip") or "").strip()
    username   = (data.get("username") or "").strip()
    if not storage_ip or not username:
        return jsonify({"ok": False, "error": "storage_ip and username are required"}), 400
    entry = storage_store.add({
        "storage_ip": storage_ip,
        "username": username,
        "name": (data.get("name") or storage_ip).strip(),
    })
    return jsonify({"ok": True, "data": entry}), 201


@app.route("/api/storage/systems/<sys_id>", methods=["DELETE"])
def storage_remove_system(sys_id):
    """Forget a saved storage system record."""
    storage_store.remove(sys_id)
    return jsonify({"ok": True})


def _storage_get(endpoint: str, params: dict = None):

    """Query the Storage Virtualize REST API.

    IBM Storage Virtualize uses HTTP POST for ALL commands (including read-only
    ones like lssystem, lshost, lsvdisk, etc.).  A GET to most endpoints returns
    405 Method Not Allowed.  We therefore always POST with an empty JSON body
    (or the optional params dict) and the auth token header.
    """
    token      = session.get("storage_token")
    storage_ip = session.get("storage_ip")
    if not token or not storage_ip:
        return {"ok": False, "error": "Not connected to storage"}, 401
    url = f"{_storage_base(storage_ip)}/{endpoint}"
    payload = params or {}
    try:
        resp = _requests.post(
            url,
            headers=_storage_headers(token),
            json=payload,
            verify=False,
            timeout=20,
        )
    except Exception as exc:
        return {"ok": False, "error": str(exc)}, 502
    if resp.status_code in (200, 201, 204):
        try:
            body = resp.json() if resp.content else []
            return {"ok": True, "data": body}, 200
        except Exception:
            return {"ok": True, "data": resp.text}, 200
    return {"ok": False, "error": f"HTTP {resp.status_code}: {resp.text[:300]}"}, resp.status_code


def _storage_post(endpoint: str, payload: dict):
    """Authenticated POST against the storage REST API."""
    token      = session.get("storage_token")
    storage_ip = session.get("storage_ip")
    if not token or not storage_ip:
        return {"ok": False, "error": "Not connected to storage"}, 401
    url = f"{_storage_base(storage_ip)}/{endpoint}"
    try:
        resp = _requests.post(url, headers=_storage_headers(token), json=payload, verify=False, timeout=30)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}, 502
    if resp.status_code in (200, 201, 204):
        try:
            body = resp.json() if resp.content else {}
        except Exception:
            body = {}
        return {"ok": True, "data": body}, resp.status_code
    return {"ok": False, "error": f"HTTP {resp.status_code}: {resp.text[:500]}"}, resp.status_code


# ── System information ─────────────────────────────────────

@app.route("/api/storage/system", methods=["GET"])
def storage_system_info():
    """Retrieve basic system information (lssystem equivalent)."""
    result, status = _storage_get("lssystem")
    return jsonify(result), status


# ── Hosts ──────────────────────────────────────────────────

@app.route("/api/storage/hosts", methods=["GET"])
def storage_list_hosts():
    """List all hosts defined on the storage system."""
    result, status = _storage_get("lshost")
    return jsonify(result), status


@app.route("/api/storage/hostports", methods=["GET"])
def storage_list_hostports():
    """List host port (WWPN / iSCSI IQN) details (lshostports).

    Returns rows containing at least: host_id, host_name, WWPN (or iscsi_name),
    type, status.  Used by the front-end to display a host's ports when its
    row is expanded in the Hosts table.
    """
    result, status = _storage_get("lshostports")
    return jsonify(result), status



@app.route("/api/storage/hosts", methods=["POST"])
def storage_add_host():
    """Create a new host object on the storage system (mkhost + addhostport).

    IBM Storage Virtualize mkhost accepts exactly ONE port identifier per call.
    Additional ports must be added with separate addhostport calls afterwards.

    Expected JSON body:
        {
            "name":     "my-server",
            "protocol": "fcscsi",          // optional connection protocol
            "type":     "generic",         // host OS type
            "wwpns":    ["500507680b23c456", ...],   // FC WWPNs
            "iqns":     ["iqn.2024-01.com.example:myserver", ...],
            "nqns":     ["nqn.2014-08.org.nvmexpress:uuid:..."],
            "iogrp":    "0:1"              // optional
        }
    """
    def _clean_wwpn(raw: str) -> str:
        """Strip separators and return uppercase bare hex digits."""
        return "".join(c for c in raw if c in "0123456789abcdefABCDEF").upper()

    data = request.get_json(force=True) or {}
    name      = (data.get("name") or "").strip()
    protocol  = (data.get("protocol") or "").strip().lower()
    host_type = (data.get("type") or "generic").strip().lower()
    raw_wwpns = [w for w in (data.get("wwpns") or []) if w.strip()]
    iqns      = [i.strip() for i in (data.get("iqns")  or []) if i.strip()]
    nqns      = [n.strip() for n in (data.get("nqns")  or []) if n.strip()]
    iogrp     = (data.get("iogrp") or "").strip()

    if not name:
        return jsonify({"ok": False, "error": "Host name is required"}), 400

    # ── Validate & normalise WWPNs ──────────────────────────────────────────
    # The IBM Storage Virtualize REST API stores and accepts WWPNs as
    # 16-char uppercase bare hex, e.g. "100070B7E428A00A" (no colons).
    wwpns = []
    for w in raw_wwpns:
        hexonly = _clean_wwpn(w)
        if len(hexonly) != 16:
            return jsonify({
                "ok": False,
                "error": (f"Invalid WWPN '{w}': must be exactly 16 hex digits "
                          f"(got {len(hexonly)}).")
            }), 400
        wwpns.append(hexonly)

    # -type only accepts these values per the mkhost syntax.
    valid_types = {"hpux", "tpgs", "generic", "adminlun"}
    if host_type not in valid_types:
        host_type = "generic"

    # ── mkhost — all WWPNs colon-joined in a single call ────────────────────
    # The REST API accepts multiple ports as a colon-separated string in one
    # mkhost call, e.g. "C0507607041A0023:C0507607041A0024".
    # force:true allows registering WWPNs not yet logged into the fabric.
    payload = {
        "name":     name,
        "type":     host_type,
        "protocol": protocol if protocol else "fcscsi",
        "force":    True,
    }
    if iogrp:
        payload["iogrp"] = iogrp

    if wwpns:
        payload["fcwwpn"] = ":".join(wwpns)
    elif iqns:
        payload["iscsiname"] = ":".join(iqns)
    elif nqns:
        payload["nqn"] = ":".join(nqns)

    result, status = _storage_post("mkhost", payload)
    if not result.get("ok"):
        return jsonify(result), status

    resp_data = result.get("data", {})
    host_id = None
    if isinstance(resp_data, dict):
        host_id = resp_data.get("id") or resp_data.get("host_id")

    return jsonify({
        "ok": True,
        "host_id": host_id,
        "name": name,
        "warnings": [],
    }), 201



@app.route("/api/storage/hosts/<host_id>", methods=["DELETE"])
def storage_remove_host(host_id):
    """Delete a host object (rmhost).

    The IBM Storage Virtualize rmhost REST API path is:
        POST /rest/v1/rmhost/{id}
    where {id} is the numeric host ID passed as the URL segment.
    """
    result, status = _storage_post(f"rmhost/{host_id}", {"force": True})
    if not result.get("ok"):
        return jsonify(result), status
    return jsonify({"ok": True, "host_id": host_id})


# ── Volumes (VDisks) ───────────────────────────────────────

@app.route("/api/storage/volumes", methods=["GET"])
def storage_list_volumes():
    """List all volumes (VDisks) on the storage system."""
    result, status = _storage_get("lsvdisk")
    return jsonify(result), status


@app.route("/api/storage/volumes", methods=["POST"])
def storage_add_volume():
    """Create a new volume from an existing storage pool (mkvolume).

    Uses the IBM Storage Virtualize ``mkvolume`` command to create an empty
    volume.  When the target pool uses a provisioning policy, the volume adopts
    the default values and capacity-saving characteristics defined in that
    policy, and the command parameters are simplified to name, size, unit and
    pool.

    mkvolume syntax (with a provisioning policy):
        mkvolume [-name name] -size disk_size [-unit b|kb|mb|gb|tb|pb]
                 -pool storage_pool_id|storage_pool_name
                 [-iogrp iogroup_id|iogroup_name]
                 [-preferrednode node_id|node_name]
                 [-volumegroup volumegroup_name|volumegroup_id|volumegroup_uuid]

    Expected JSON body:
        {
            "name":          "my-vol-01",   // optional volume name
            "size":          100,           // capacity (required)
            "unit":          "gb",          // b|kb|mb|gb|tb|pb (default gb)
            "pool":          "Pool0",       // storage pool id/name (required)
            "iogrp":         "0",           // optional I/O group id/name
            "preferrednode": "node1",       // optional preferred node id/name
            "volumegroup":   "vg1"          // optional volume group id/name/uuid
        }
    """
    data = request.get_json(force=True) or {}
    name          = (data.get("name") or "").strip()
    pool          = (data.get("pool") or "").strip()
    unit          = (data.get("unit") or "gb").strip().lower()
    iogrp         = (data.get("iogrp") or "").strip()
    preferrednode = (data.get("preferrednode") or "").strip()
    volumegroup   = (data.get("volumegroup") or "").strip()
    try:
        size = int(data.get("size", 0))
    except (TypeError, ValueError):
        size = 0

    valid_units = {"b", "kb", "mb", "gb", "tb", "pb"}
    if unit not in valid_units:
        unit = "gb"

    if not pool:
        return jsonify({"ok": False, "error": "Storage pool name is required"}), 400
    if size <= 0:
        return jsonify({"ok": False, "error": "Size must be a positive integer"}), 400

    payload = {
        "size": str(size),
        "unit": unit,
        "pool": pool,
    }
    if name:
        payload["name"] = name
    if iogrp:
        payload["iogrp"] = iogrp
    if preferrednode:
        payload["preferrednode"] = preferrednode
    if volumegroup:
        payload["volumegroup"] = volumegroup

    result, status = _storage_post("mkvolume", payload)
    return jsonify(result), status


# ── Storage Pools ──────────────────────────────────────────

@app.route("/api/storage/pools", methods=["GET"])
def storage_list_pools():
    """List all MDisk groups (storage pools)."""
    result, status = _storage_get("lsmdiskgrp")
    return jsonify(result), status


# ── Host–Volume Mapping ────────────────────────────────────

@app.route("/api/storage/mappings", methods=["GET"])
def storage_list_mappings():
    """List all host–volume mappings (lshostvdiskmap).

    The IBM Storage Virtualize REST API returns lshostvdiskmap rows with these fields:
      id, name (vdisk name), SCSI_id, host_id, host_name, vdisk_id, vdisk_name,
      vdisk_UID, IO_group_id, IO_group_name, mapping_type, fc_id, fc_name, rc_id,
      rc_name, se_copy_id, copy_id, lun_id, ...
    We return the raw list and let the frontend normalise field names.
    """
    result, status = _storage_get("lshostvdiskmap")
    return jsonify(result), status


@app.route("/api/storage/mappings/debug", methods=["GET"])
def storage_mappings_debug():
    """Debug endpoint — returns raw lshostvdiskmap response so field names can be inspected."""
    result, status = _storage_get("lshostvdiskmap")
    return jsonify(result), status


@app.route("/api/storage/mappings", methods=["POST"])
def storage_map_volume():
    """Map a volume to a host (mkvdiskhostmap).

    IBM Storage Virtualize REST API syntax:
        POST /rest/v1/mkvdiskhostmap/<vdisk_name_or_id>
    with the host name/ID and optional parameters in the JSON body.

    mkvdiskhostmap syntax:
        mkvdiskhostmap [-force] -host <host_id|host_name>
                       [-scsi scsi_num] [-allowmismatchedscsiids]
                       <vdisk_name|vdisk_id>

    Expected JSON body:
        {
            "host":   "my-server",     // host name or id
            "volume": "my-vol-01"      // volume (vdisk) name or id
        }
    """
    data = request.get_json(force=True) or {}
    host   = (data.get("host")   or "").strip()
    volume = (data.get("volume") or "").strip()

    if not host:
        return jsonify({"ok": False, "error": "Host name is required"}), 400
    if not volume:
        return jsonify({"ok": False, "error": "Volume name is required"}), 400

    payload = {"host": host, "force": True}
    result, status = _storage_post(f"mkvdiskhostmap/{volume}", payload)
    return jsonify(result), status


@app.route("/api/storage/mappings/remove", methods=["POST"])
def storage_unmap_volume():
    """Unmap a volume from a host (rmvdiskhostmap).

    IBM Storage Virtualize REST API syntax:
        POST /rest/v1/rmvdiskhostmap/<vdisk_name_or_id>
    with the host name/ID in the JSON body.

    rmvdiskhostmap syntax:
        rmvdiskhostmap -host <host_id|host_name> <vdisk_id|vdisk_name>

    Expected JSON body:
        {
            "host":   "my-server",     // host name or id
            "volume": "my-vol-01"      // volume (vdisk) name or id
        }
    """
    data = request.get_json(force=True) or {}
    host   = (data.get("host")   or "").strip()
    volume = (data.get("volume") or "").strip()

    if not host:
        return jsonify({"ok": False, "error": "Host name is required"}), 400
    if not volume:
        return jsonify({"ok": False, "error": "Volume name is required"}), 400

    payload = {"host": host}
    result, status = _storage_post(f"rmvdiskhostmap/{volume}", payload)
    return jsonify(result), status


# ──────────────────────────────────────────────────────────
# Test operations — VIOS resilience test on a target LPAR
# ──────────────────────────────────────────────────────────
#
# Flow (all driven from the Test GUI page):
#   1. Deploy the vios_res_client.sh script to the target LPAR (SFTP).
#   2. Run the test remotely:  vios_res_client.sh test <label> <mode> <secs>
#      which packages the evidence into /tmp/vios_res_<host>_<ts>.tar.gz.
#   3. Download that tar.gz into the local testresult/ folder.
#   4. Generate a consolidated Word report from every result in testresult/.
#   5. Download the report / individual results.
# ──────────────────────────────────────────────────────────

TESTRESULT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "testresult")
VIOS_RES_SCRIPT = os.path.join(SCRIPTS_DIR, "vios_res_client.sh")


@app.route("/api/test/deploy", methods=["POST"])
def test_deploy():
    """Deploy vios_res_client.sh to a target LPAR via SFTP.

    The actual test run (which requires an operator to shut down/restart the
    VIOS on the HMC at the right moment) must be run manually by the user in
    an SSH/console session — this route only pushes the script and returns
    the exact command the user should run themselves, plus the target dir so
    the "Fetch Results" step later knows where to look.

    Body: {host, username, password, port, label, mode (shutdown|restart),
           seconds, target_path}
    """
    data = request.get_json(force=True) or {}
    host      = (data.get("host") or "").strip()
    username  = (data.get("username") or "root").strip()
    password  = data.get("password") or ""
    label     = (data.get("label") or "").strip() or "test"
    mode      = (data.get("mode") or "shutdown").strip().lower()
    target_dir = (data.get("target_path") or "/tmp").strip().rstrip("/") or "/tmp"
    try:
        port = int(data.get("port") or 22)
    except (TypeError, ValueError):
        port = 22
    try:
        seconds = int(data.get("seconds") or 180)
    except (TypeError, ValueError):
        seconds = 180

    if not host:
        return jsonify({"ok": False, "error": "Target host is required"}), 400
    if mode not in ("shutdown", "restart"):
        mode = "shutdown"
    if not os.path.isfile(VIOS_RES_SCRIPT):
        return jsonify({"ok": False, "error": "vios_res_client.sh not found in scripts/"}), 404

    steps = []
    remote_script = f"{target_dir}/vios_res_client.sh"

    # ── Step 1: deploy the script ─────────────────────────────
    dep = ssh_manager.deploy_file(
        local_path=VIOS_RES_SCRIPT, host=host, port=port,
        username=username, password=password,
        target_path=remote_script, method="sftp",
    )
    steps.append({"step": 1, "label": "Deploy vios_res_client.sh",
                  "ok": dep.get("ok", False),
                  "output": dep.get("message", ""),
                  "error": dep.get("error", "")})
    if not dep.get("ok"):
        return jsonify({"ok": False, "error": f"Deploy failed: {dep.get('error')}",
                        "steps": steps}), 400

    # Build the exact command the operator should run manually on the LPAR
    # (via SSH/console) once they are ready to perform the VIOS shutdown or
    # restart at the correct moment.
    manual_cmd = (f'cd "{target_dir}" && chmod +x vios_res_client.sh && '
                  f'./vios_res_client.sh test "{label}" {mode} {seconds}')

    return jsonify({
        "ok": True,
        "message": "Script deployed. Run the command below manually on the target LPAR.",
        "steps": steps,
        "manual_command": manual_cmd,
        "target_dir": target_dir,
        "mode": mode,
    })


@app.route("/api/test/fetch", methods=["POST"])
def test_fetch():
    """Locate the newest vios_res_*.tar.gz on the target host and download it
    into testresult/. Call this AFTER the manual test run (deploy step) has
    completed on the LPAR.

    Body: {host, username, password, port}
    """
    data = request.get_json(force=True) or {}
    host      = (data.get("host") or "").strip()
    username  = (data.get("username") or "root").strip()
    password  = data.get("password") or ""
    try:
        port = int(data.get("port") or 22)
    except (TypeError, ValueError):
        port = 22

    if not host:
        return jsonify({"ok": False, "error": "Target host is required"}), 400

    steps = []

    # ── Step: find the generated tar.gz on the remote host ────
    find_cmd = "ls -1t /tmp/vios_res_*.tar.gz 2>/dev/null | head -1"
    found = ssh_manager.run_command(
        host=host, port=port, username=username, password=password,
        command=find_cmd, timeout=60,
    )
    remote_tar = (found.get("output") or "").strip().splitlines()
    remote_tar = remote_tar[0].strip() if remote_tar else ""
    if not remote_tar:
        steps.append({"step": 1, "label": "Locate result tar.gz", "ok": False,
                      "output": "", "error": "No vios_res_*.tar.gz found on target"})
        return jsonify({"ok": False, "error": "No tar.gz found on target. "
                        "Has the manual test run completed?", "steps": steps}), 400
    steps.append({"step": 1, "label": "Locate result tar.gz", "ok": True,
                  "output": remote_tar, "error": ""})

    # ── Step: download it ──────────────────────────────────────
    os.makedirs(TESTRESULT_DIR, exist_ok=True)
    local_name = os.path.basename(remote_tar)
    local_tar = os.path.join(TESTRESULT_DIR, local_name)
    dl = ssh_manager.download_file(
        host=host, port=port, username=username, password=password,
        remote_path=remote_tar, local_path=local_tar,
    )
    steps.append({"step": 2, "label": "Download result tar.gz",
                  "ok": dl.get("ok", False),
                  "output": dl.get("message", ""),
                  "error": dl.get("error", "")})
    if not dl.get("ok"):
        return jsonify({"ok": False, "error": f"Download failed: {dl.get('error')}",
                        "steps": steps}), 400

    return jsonify({
        "ok": True,
        "message": f"Results downloaded to testresult/{local_name}",
        "tar_file": local_name,
        "steps": steps,
    })


@app.route("/api/test/results", methods=["GET"])
def test_results():
    """List downloaded test result archives and any generated reports."""
    os.makedirs(TESTRESULT_DIR, exist_ok=True)
    archives = []
    reports = []
    for entry in sorted(os.listdir(TESTRESULT_DIR)):
        full = os.path.join(TESTRESULT_DIR, entry)
        if not os.path.isfile(full):
            continue
        info = {"name": entry, "size": os.path.getsize(full)}
        if entry.endswith(".tar.gz"):
            archives.append(info)
        elif entry.endswith(".docx"):
            reports.append(info)
    return jsonify({"ok": True, "archives": archives, "reports": reports})


@app.route("/api/test/report", methods=["POST"])
def test_report():
    """Generate the consolidated VIOS resilience Word report from every
    archive/result folder currently in testresult/ (runs generate_vios_report.py)."""
    report_script = os.path.join(SCRIPTS_DIR, "generate_vios_report.py")
    if not os.path.isfile(report_script):
        return jsonify({"ok": False, "error": "generate_vios_report.py not found"}), 404
    os.makedirs(TESTRESULT_DIR, exist_ok=True)
    try:
        proc = subprocess.run(
            ["python3", report_script],
            capture_output=True, text=True, timeout=600,
            cwd=os.path.dirname(os.path.abspath(__file__)),
        )
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "Report generation timed out"}), 504
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500

    if proc.returncode != 0:
        return jsonify({"ok": False,
                        "error": proc.stderr.strip() or proc.stdout.strip()
                        or "Report generation failed",
                        "output": proc.stdout}), 400

    report_name = "VIOS_Resilience_Report.docx"
    return jsonify({
        "ok": True,
        "message": "Report generated.",
        "report": report_name,
        "output": proc.stdout,
    })


@app.route("/api/test/download/<path:filename>", methods=["GET"])
def test_download(filename):
    """Download a result archive or report from the testresult/ folder."""
    safe_name = os.path.basename(filename)
    full = os.path.join(TESTRESULT_DIR, safe_name)
    if not os.path.isfile(full):
        return jsonify({"ok": False, "error": "File not found"}), 404
    return send_from_directory(TESTRESULT_DIR, safe_name, as_attachment=True)


@app.route("/api/test/delete/<path:filename>", methods=["DELETE"])
def test_delete(filename):
    """Delete a result archive (.tar.gz) or report (.docx) from testresult/.

    Only files directly inside TESTRESULT_DIR are removable (basename-only,
    no path traversal). If the file is a .tar.gz archive, its already-extracted
    sibling folder (same name minus .tar.gz) is removed too, if present.
    """
    safe_name = os.path.basename(filename)
    full = os.path.join(TESTRESULT_DIR, safe_name)
    if not os.path.isfile(full):
        return jsonify({"ok": False, "error": "File not found"}), 404
    try:
        os.remove(full)
        # Clean up the extracted directory that matches a deleted archive.
        if safe_name.endswith(".tar.gz"):
            extracted_dir = os.path.join(TESTRESULT_DIR, safe_name[:-len(".tar.gz")])
            if os.path.isdir(extracted_dir):
                import shutil
                shutil.rmtree(extracted_dir, ignore_errors=True)
    except OSError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    return jsonify({"ok": True, "message": f"Deleted {safe_name}"})


# ──────────────────────────────────────────────────────────
# NIM Management API
# ──────────────────────────────────────────────────────────


_NIM_SAFE_VALUE = re.compile(r"^[A-Za-z0-9_.:-]+$")
_NIM_MAC_ADDRESS = re.compile(r"^[0-9A-Fa-f]{12}$")


def _nim_run_server_command(server: dict, command: str, timeout: int = 60) -> dict:
    """Run a command on a configured NIM master."""
    return ssh_manager.run_command(
        host=server["host"],
        port=int(server.get("ssh_port", 22)),
        username=server.get("username", "root"),
        key_path=server.get("key_path") or None,
        password=server.get("password") or None,
        command=command,
        timeout=timeout,
    )


def _nim_safe_value(value, label: str) -> str:
    """Validate values that will be put in a NIM command."""
    value = (value or "").strip()
    if not value or not _NIM_SAFE_VALUE.fullmatch(value):
        raise ValueError(f"Invalid {label}")
    return value


def _nim_remote_hosts_contains(result: dict, ip_address: str, hostname: str) -> bool:
    """Return whether an `awk` hosts lookup returned the requested mapping."""
    return result.get("ok", False) and result.get("output", "").strip() == "FOUND"


def _nim_hosts_lookup_command(ip_address: str, hostname: str) -> str:
    """Build a shell-safe exact IP/hostname lookup against /etc/hosts."""
    return (
        f"awk '$1 == \"{ip_address}\" {{ for (i=2; i<=NF; i++) if ($i == \"{hostname}\") {{ print \"FOUND\"; exit }} }}' "
        "/etc/hosts"
    )


def _nim_client_prerequisites(client_kwargs: dict) -> dict:
    """Read mandatory local client prerequisites without changing the client."""
    command = r'''if command -v niminit >/dev/null 2>&1; then echo '__PP_NIMINIT__=OK'; else echo '__PP_NIMINIT__=MISSING'; fi
if lslpp -l bos.sysmgt.nim.client 2>/dev/null | awk '$1 == "bos.sysmgt.nim.client" && $3 == "COMMITTED" { found=1 } END { exit !found }'; then echo '__PP_NIMCLIENT__=OK'; else echo '__PP_NIMCLIENT__=MISSING'; fi
if [ -e /etc/niminfo ]; then echo '__PP_NIMINFO__=EXISTS'; else echo '__PP_NIMINFO__=ABSENT'; fi
exit 0'''
    result = ssh_manager.run_command(command=command, **client_kwargs)
    output = result.get("output", "")
    values = dict(re.findall(r"__PP_([A-Z]+)__=([A-Z]+)", output))
    return {
        "command_result": result,
        "niminit": values.get("NIMINIT") == "OK",
        "nim_client_fileset": values.get("NIMCLIENT") == "OK",
        "niminfo_absent": values.get("NIMINFO") == "ABSENT",
    }


def _nim_mksysb_client_preflight(client_kwargs: dict) -> dict:
    """Read mksysb capability and total mounted filesystem usage on a client."""
    command = r'''if command -v mksysb >/dev/null 2>&1; then echo '__PP_MKSYSB__=OK'; else echo '__PP_MKSYSB__=MISSING'; fi
if lsvg rootvg >/dev/null 2>&1; then
  echo '__PP_ROOTVG__=OK'
  df -g | awk '
    NR > 1 && $2 ~ /^[0-9]+(\.[0-9]+)?$/ && $3 ~ /^[0-9]+(\.[0-9]+)?$/ {
      used_gb += $2 - $3; filesystem_count++
    }
    END {
      if (filesystem_count > 0) print "__PP_CLIENT_USED_GB__=" used_gb
    }'
else
  echo '__PP_ROOTVG__=MISSING'
fi
exit 0'''
    result = ssh_manager.run_command(command=command, **client_kwargs)
    output = result.get("output", "")
    values = dict(re.findall(r"__PP_([A-Z_]+)__=([A-Z]+)", output))
    used_match = re.search(r"__PP_CLIENT_USED_GB__=([0-9]+(?:\.[0-9]+)?)", output)
    return {
        "command_result": result,
        "mksysb": values.get("MKSYSB") == "OK",
        "rootvg": values.get("ROOTVG") == "OK",
        "client_used_gb": float(used_match.group(1)) if used_match else None,
    }


def _nim_mksysb_resource_preflight(srv: dict, resource_name: str, location: str) -> dict:
    """Read master-side mksysb destination state without modifying it."""
    command = (
        f"if [ -d {shlex.quote(location)} ] && [ -w {shlex.quote(location)} ]; then "
        "echo '__PP_DESTINATION__=OK'; "
        rf"df -g {shlex.quote(location)} | awk '$2 ~ /^[0-9]+(\.[0-9]+)?$/ && $3 ~ /^[0-9]+(\.[0-9]+)?$/ {{ print "
        "\"__PP_FREE_GB__=\" $3; exit }'; "
        f"else echo '__PP_DESTINATION__=INVALID'; fi\n"
        f"if lsnim {shlex.quote(resource_name)} >/dev/null 2>&1; then echo '__PP_RESOURCE__=EXISTS'; "
        "else echo '__PP_RESOURCE__=ABSENT'; fi\n"
        f"if [ -e {shlex.quote(location.rstrip('/') + '/' + resource_name)} ]; then echo '__PP_IMAGE__=EXISTS'; "
        "else echo '__PP_IMAGE__=ABSENT'; fi\nexit 0"
    )
    result = _nim_run_server_command(srv, command, timeout=30)
    output = result.get("output", "")
    values = dict(re.findall(r"__PP_([A-Z_]+)__=([A-Z]+)", output))
    free_match = re.search(r"__PP_FREE_GB__=([0-9]+(?:\.[0-9]+)?)", output)
    free_gb = float(free_match.group(1)) if free_match else None
    return {
        "command_result": result,
        "destination": values.get("DESTINATION") == "OK",
        "resource_absent": values.get("RESOURCE") == "ABSENT",
        "image_absent": values.get("IMAGE") == "ABSENT",
        "free_gb": free_gb,
        "free_kb": int(free_gb * 1024 * 1024) if free_gb is not None else None,
    }


def _nim_mksysb_payload(data: dict) -> tuple:
    """Validate request-only client login and master resource fields."""
    client_name = _nim_safe_value(data.get("client_name"), "NIM client name")
    resource_name = _nim_safe_value(data.get("resource_name"), "mksysb resource name")
    client_host = _nim_safe_value(data.get("client_host"), "client host")
    location = (data.get("location") or "").strip()
    if not location.startswith("/") or "\x00" in location or "\n" in location or "\r" in location:
        raise ValueError("Backup location must be an absolute path without line breaks")
    client_port = int(data.get("client_port") or 22)
    if not 1 <= client_port <= 65535:
        raise ValueError("Invalid client SSH port")
    client_user = (data.get("client_username") or "root").strip()
    if not client_user:
        raise ValueError("Client SSH username is required")
    client_kwargs = dict(
        host=client_host, port=client_port, username=client_user,
        key_path=(data.get("client_key_path") or "").strip() or None,
        password=data.get("client_password") or None, timeout=30,
    )
    return client_name, resource_name, location.rstrip("/"), client_kwargs


@app.route("/api/nim/mksysb/preflight", methods=["POST"])
def nim_mksysb_preflight():
    """Check NIM mksysb creation prerequisites without changing either system."""
    data = request.get_json(force=True) or {}
    srv = nim_store.get_server((data.get("server_id") or "").strip())
    if not srv:
        return jsonify({"ok": False, "error": "NIM server not found"}), 404
    try:
        client_name, resource_name, location, client_kwargs = _nim_mksysb_payload(data)
    except (TypeError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    checks = []
    master_nim = _nim_run_server_command(srv, "command -v nim >/dev/null 2>&1", timeout=30)
    checks.append({"name": "NIM command available on master", "ok": master_nim.get("ok", False),
                   "detail": master_nim.get("stderr", "") or master_nim.get("error", "")})
    master_state = _nim_mksysb_resource_preflight(srv, resource_name, location)
    checks.extend([
        {"name": f"Backup location is writable ({location})", "ok": master_state["destination"],
         "detail": "Create the directory and make it writable by the NIM master root user."},
        {"name": f"NIM resource name is unused ({resource_name})", "ok": master_state["resource_absent"],
         "detail": "Choose another resource name or remove the existing NIM resource."},
        {"name": f"Backup image path is unused ({location}/{resource_name})", "ok": master_state["image_absent"],
         "detail": "Choose another resource name or move/remove the existing image."},
    ])
    client_state = _nim_mksysb_client_preflight(client_kwargs)
    client_result = client_state["command_result"]
    checks.extend([
        {"name": "AIX client reachable by SSH", "ok": client_result.get("ok", False),
         "detail": client_result.get("error", "") or client_result.get("stderr", "")},
        {"name": "mksysb command installed on client", "ok": client_state["mksysb"], "detail": "Install the AIX base operating system backup utilities if missing."},
        {"name": "rootvg is available on client", "ok": client_state["rootvg"], "detail": "mksysb only backs up rootvg."},
    ])
    nim_client = _nim_run_server_command(srv, f"lsnim -l {shlex.quote(client_name)}", timeout=30)
    checks.append({"name": f"NIM client exists on master ({client_name})", "ok": nim_client.get("ok", False),
                   "detail": nim_client.get("stderr", "") or nim_client.get("error", "")})
    client_used_gb = client_state["client_used_gb"]
    free_gb = master_state["free_gb"]
    required_gb = client_used_gb * 1.15 if client_used_gb is not None else None
    enough_space = required_gb is not None and free_gb is not None and free_gb >= required_gb
    if required_gb is not None and free_gb is not None:
        space_detail = (f"Total client filesystem usage (`df -g`): {client_used_gb:.2f} GB; "
                        f"required with 15% margin: {required_gb:.2f} GB; "
                        f"master target free space (`df -g {location}`): {free_gb:.2f} GB.")
    elif client_used_gb is None and free_gb is None:
        space_detail = ("Could not parse total client filesystem usage (`df -g`) or master target free capacity "
                        f"(`df -g {location}`).")
    elif client_used_gb is None:
        space_detail = (f"Master target free space (`df -g {location}`): {free_gb:.2f} GB, but total client filesystem usage could not be parsed from "
                        "`df -g` on the AIX client.")
    else:
        space_detail = (f"Total client filesystem usage (`df -g`): {client_used_gb:.2f} GB, but free capacity could not be parsed from "
                        f"`df -g {location}` on the NIM master.")
    checks.append({"name": "Master destination has sufficient free space", "ok": enough_space, "detail": space_detail})
    return jsonify({"ok": True, "data": {"ready": all(check["ok"] for check in checks), "checks": checks,
                                        "client_used_gb": client_used_gb, "required_gb": required_gb,
                                        "available_gb": free_gb}})


@app.route("/api/nim/mksysb/create", methods=["POST"])
def nim_create_mksysb():
    """Define a NIM mksysb resource and create its image from a client."""
    data = request.get_json(force=True) or {}
    srv = nim_store.get_server((data.get("server_id") or "").strip())
    if not srv:
        return jsonify({"ok": False, "error": "NIM server not found"}), 404
    try:
        client_name, resource_name, location, client_kwargs = _nim_mksysb_payload(data)
    except (TypeError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    master_state = _nim_mksysb_resource_preflight(srv, resource_name, location)
    client_state = _nim_mksysb_client_preflight(client_kwargs)
    client_used_gb = client_state["client_used_gb"]
    required_gb = client_used_gb * 1.15 if client_used_gb is not None else None
    if (not master_state["destination"] or not master_state["resource_absent"] or not master_state["image_absent"]
            or not client_state["command_result"].get("ok") or not client_state["mksysb"] or not client_state["rootvg"]
            or required_gb is None or master_state["free_gb"] is None or master_state["free_gb"] < required_gb):
        return jsonify({"ok": False, "error": "mksysb preflight no longer passes; no NIM resource was created. Run preflight again."}), 409
    client_exists = _nim_run_server_command(srv, f"lsnim {shlex.quote(client_name)}", timeout=30)
    if not client_exists.get("ok"):
        return jsonify({"ok": False, "error": "NIM client no longer exists on the master; no mksysb resource was created."}), 409

    image_path = f"{location}/{resource_name}"
    define_command = (f"nim -o define -t mksysb -a server=master -a location={shlex.quote(image_path)} "
                      f"{shlex.quote(resource_name)}")
    define_result = _nim_run_server_command(srv, define_command, timeout=60)
    steps = [{"name": "Define mksysb resource on NIM master", "command": define_command,
              "ok": define_result.get("ok", False), "output": define_result.get("output", ""),
              "stderr": define_result.get("stderr", "") or define_result.get("error", "")}]
    if not define_result.get("ok"):
        return jsonify({"ok": False, "error": "NIM master could not define the mksysb resource", "steps": steps}), 502
    create_command = f"nim -o create -a source={shlex.quote(client_name)} {shlex.quote(resource_name)}"
    create_result = _nim_run_server_command(srv, create_command, timeout=7200)
    steps.append({"name": "Create mksysb image from client", "command": create_command,
                  "ok": create_result.get("ok", False), "output": create_result.get("output", ""),
                  "stderr": create_result.get("stderr", "") or create_result.get("error", "")})
    if not create_result.get("ok"):
        return jsonify({"ok": False, "error": "The mksysb resource was defined, but image creation failed. Review the output; remove the resource if you will not retry.", "steps": steps}), 502
    verify_command = f"lsnim -l {shlex.quote(resource_name)}"
    verify_result = _nim_run_server_command(srv, verify_command, timeout=30)
    steps.append({"name": "Verify mksysb resource", "command": verify_command,
                  "ok": verify_result.get("ok", False), "output": verify_result.get("output", ""),
                  "stderr": verify_result.get("stderr", "") or verify_result.get("error", "")})
    if not verify_result.get("ok"):
        return jsonify({"ok": False, "error": "mksysb image creation completed but resource verification failed", "steps": steps}), 502
    return jsonify({"ok": True, "resource_name": resource_name, "image_path": image_path, "steps": steps}), 201


@app.route("/api/nim/clients/preflight", methods=["POST"])
def nim_preflight_client():
    """Check NIMSH onboarding prerequisites without changing either system."""
    data = request.get_json(force=True) or {}
    srv = nim_store.get_server((data.get("server_id") or "").strip())
    if not srv:
        return jsonify({"ok": False, "error": "NIM server not found"}), 404
    try:
        client_name = _nim_safe_value(data.get("client_name"), "client name")
        client_host = _nim_safe_value(data.get("client_host"), "client host")
        network = _nim_safe_value(data.get("network"), "NIM network")
        client_port = int(data.get("client_port") or 22)
        if not 1 <= client_port <= 65535:
            raise ValueError("Invalid client SSH port")
    except (TypeError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    client_kwargs = dict(host=client_host, port=client_port,
                         username=(data.get("client_username") or "root").strip(),
                         key_path=(data.get("client_key_path") or "").strip() or None,
                         password=data.get("client_password") or None, timeout=30)
    master_name_result = _nim_run_server_command(srv, "hostname", timeout=30)
    if not master_name_result.get("ok") or not master_name_result.get("output", "").strip():
        return jsonify({"ok": False, "error": "Could not determine NIM master hostname"}), 502
    try:
        master_name = _nim_safe_value(master_name_result["output"].splitlines()[0], "NIM master hostname")
    except (IndexError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502

    checks = []
    master_nim = _nim_run_server_command(srv, "command -v nim >/dev/null 2>&1", timeout=30)
    checks.append({"name": "NIM command available on master", "ok": master_nim.get("ok", False),
                   "detail": master_nim.get("stderr", "") or master_nim.get("error", "")})
    network_check = _nim_run_server_command(srv, f"lsnim -l {shlex.quote(network)}", timeout=30)
    checks.append({"name": f"Selected NIM network exists ({network})", "ok": network_check.get("ok", False),
                   "detail": network_check.get("stderr", "") or network_check.get("error", "")})
    prerequisites = _nim_client_prerequisites(client_kwargs)
    client_command = prerequisites["command_result"]
    checks.extend([
        {"name": "AIX client reachable by SSH", "ok": client_command.get("ok", False),
         "detail": client_command.get("error", "") or client_command.get("stderr", "")},
        {"name": "niminit command installed on client", "ok": prerequisites["niminit"], "detail": "Install bos.sysmgt.nim.client if missing."},
        {"name": "bos.sysmgt.nim.client fileset committed", "ok": prerequisites["nim_client_fileset"], "detail": ""},
        {"name": "/etc/niminfo is absent on client", "ok": prerequisites["niminfo_absent"],
         "detail": "Remove the stale file with: rm -f /etc/niminfo, then run preflight again."},
    ])
    for target, command, description in (
        ("NIM master", _nim_hosts_lookup_command(client_host, client_name), f"Master /etc/hosts has {client_host} {client_name}"),
        ("AIX client", _nim_hosts_lookup_command(srv["host"], master_name), f"Client /etc/hosts has {srv['host']} {master_name}"),
    ):
        result = _nim_run_server_command(srv, command, timeout=30) if target == "NIM master" else ssh_manager.run_command(command=command, **client_kwargs)
        checks.append({"name": description, "ok": _nim_remote_hosts_contains(result, "", ""),
                       "detail": result.get("error", "") or result.get("stderr", "") or "Add it in the Hosts step if missing."})
    # Host entries may be deliberately missing at this point: the wizard's
    # Hosts step can add them after explicit confirmation. All other checks
    # must pass before the define/niminit operation may begin.
    return jsonify({"ok": True, "data": {"ready": all(check["ok"] for check in checks[:6]), "checks": checks}})


@app.route("/api/nim/clients/hosts-check", methods=["POST"])
def nim_check_client_hosts():
    """Check required master/client name mappings without changing either host."""
    data = request.get_json(force=True) or {}
    srv = nim_store.get_server((data.get("server_id") or "").strip())
    if not srv:
        return jsonify({"ok": False, "error": "NIM server not found"}), 404
    try:
        client_name = _nim_safe_value(data.get("client_name"), "client name")
        client_host = _nim_safe_value(data.get("client_host"), "client host")
        master_host = _nim_safe_value(srv["host"], "NIM master host")
        client_port = int(data.get("client_port") or 22)
        if not 1 <= client_port <= 65535:
            raise ValueError("Invalid client SSH port")
    except (TypeError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    master_name_result = _nim_run_server_command(srv, "hostname", timeout=30)
    if not master_name_result.get("ok"):
        return jsonify({"ok": False, "error": "Could not determine NIM master hostname"}), 502
    try:
        master_name = _nim_safe_value(master_name_result.get("output", "").splitlines()[0], "NIM master hostname")
    except (IndexError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502

    client_kwargs = dict(
        host=client_host, port=client_port,
        username=(data.get("client_username") or "root").strip(),
        key_path=(data.get("client_key_path") or "").strip() or None,
        password=data.get("client_password") or None, timeout=30,
    )
    master_has_client = _nim_run_server_command(srv, _nim_hosts_lookup_command(client_host, client_name), timeout=30)
    client_has_master = ssh_manager.run_command(command=_nim_hosts_lookup_command(master_host, master_name), **client_kwargs)
    if not master_has_client.get("ok") or not client_has_master.get("ok"):
        return jsonify({"ok": False, "error": "Could not read /etc/hosts on the NIM master or client"}), 502

    missing = []
    if not _nim_remote_hosts_contains(master_has_client, client_host, client_name):
        missing.append({"target": "NIM master", "ip": client_host, "hostname": client_name})
    if not _nim_remote_hosts_contains(client_has_master, master_host, master_name):
        missing.append({"target": "AIX client", "ip": master_host, "hostname": master_name})
    return jsonify({"ok": True, "data": {"master_name": master_name, "missing": missing}})


@app.route("/api/nim/clients/discover", methods=["POST"])
def nim_discover_client():
    """Connect to an existing AIX client and discover NIM define attributes.

    Credentials are used only for this request and are never persisted.
    """
    data = request.get_json(force=True) or {}
    host = (data.get("host") or "").strip()
    username = (data.get("username") or "root").strip()
    if not host or not username:
        return jsonify({"ok": False, "error": "Client host and SSH username are required"}), 400

    try:
        port = int(data.get("port") or 22)
        if not 1 <= port <= 65535:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Invalid SSH port"}), 400

    # The markers make the response robust against normal command output.
    command = r'''printf '__PP_HOSTNAME__='; hostname
printf '__PP_PLATFORM__='; uname -M
printf '__PP_KERNEL__='; getconf KERNEL_BITMODE 2>/dev/null || getconf KERNEL_BITMODE
printf '__PP_OSLEVEL__='; oslevel -s 2>/dev/null
for adapter in $(lsdev -Cc adapter 2>/dev/null | awk '$1 ~ /^ent[0-9]+$/ {print $1}'); do
  printf '__PP_ADAPTER__=%s|' "$adapter"
  entstat -d "$adapter" 2>/dev/null | awk '
    /[Hh]ardware [Aa]ddress/ {
      if (match($0, /[0-9A-Fa-f][0-9A-Fa-f](:[0-9A-Fa-f][0-9A-Fa-f]){5}/)) {
        print substr($0, RSTART, RLENGTH); exit
      }
    }'
done'''
    result = ssh_manager.run_command(
        host=host, port=port, username=username,
        key_path=(data.get("key_path") or "").strip() or None,
        password=data.get("password") or None,
        command=command, timeout=45,
    )
    if not result.get("ok"):
        return jsonify({"ok": False, "error": result.get("error") or result.get("stderr") or "Client discovery failed"})

    discovered = {"hostname": "", "platform": "", "kernel": "", "oslevel": "", "adapters": []}
    for line in result.get("output", "").splitlines():
        line = line.strip()
        if line.startswith("__PP_HOSTNAME__="):
            discovered["hostname"] = line.split("=", 1)[1].strip()
        elif line.startswith("__PP_PLATFORM__="):
            discovered["platform"] = line.split("=", 1)[1].strip()
        elif line.startswith("__PP_KERNEL__="):
            discovered["kernel"] = line.split("=", 1)[1].strip()
        elif line.startswith("__PP_OSLEVEL__="):
            discovered["oslevel"] = line.split("=", 1)[1].strip()
        elif line.startswith("__PP_ADAPTER__="):
            adapter, _, mac = line.split("=", 1)[1].partition("|")
            normalized_mac = re.sub(r"[^0-9A-Fa-f]", "", mac).upper()
            # NIM's if1 attribute requires exactly six MAC octets. Do not
            # convert arbitrary adapter-status text into a purported address.
            discovered["adapters"].append({
                "name": adapter.strip(),
                "mac": normalized_mac if _NIM_MAC_ADDRESS.fullmatch(normalized_mac) else "",
            })

    if not discovered["hostname"]:
        return jsonify({"ok": False, "error": "Could not determine client hostname"})
    return jsonify({"ok": True, "data": discovered})


@app.route("/api/nim/servers/<server_id>/networks", methods=["GET"])
def nim_server_networks(server_id):
    """List NIM network objects available on the selected master."""
    srv = nim_store.get_server(server_id)
    if not srv:
        return jsonify({"ok": False, "error": "NIM server not found"}), 404
    result = _nim_run_server_command(srv, "lsnim -l -t ent", timeout=30)
    output = result.get("output", "")
    if not output and not result.get("ok"):
        return jsonify({"ok": False, "error": result.get("error") or result.get("stderr") or "Could not list NIM networks"})
    return jsonify({"ok": True, "data": _parse_lsnim_stanzas(output)})

@app.route("/api/nim/servers", methods=["GET"])
def nim_list_servers():
    """Return all saved NIM servers (passwords stripped)."""
    return jsonify(nim_store.list_servers())


@app.route("/api/nim/servers", methods=["POST"])
def nim_add_server():
    """Save a new NIM server entry."""
    data = request.get_json(force=True)
    required = ["name", "host"]
    missing = [f for f in required if not data.get(f)]
    if missing:
        return jsonify({"error": f"Missing fields: {', '.join(missing)}"}), 400
    entry = nim_store.add_server(data)
    return jsonify(entry), 201


@app.route("/api/nim/servers/<server_id>", methods=["DELETE"])
def nim_delete_server(server_id):
    """Delete a NIM server and all its associated clients."""
    nim_store.remove_server(server_id)
    return jsonify({"ok": True})


@app.route("/api/nim/servers/<server_id>/test", methods=["POST"])
def nim_test_server(server_id):
    """Test SSH connectivity to a NIM server."""
    srv = nim_store.get_server(server_id)
    if not srv:
        return jsonify({"error": "NIM server not found"}), 404
    result = ssh_manager.test_connection(
        host=srv["host"],
        port=int(srv.get("ssh_port", 22)),
        username=srv.get("username", "root"),
        key_path=srv.get("key_path") or None,
        password=srv.get("password") or None,
    )
    return jsonify(result)


@app.route("/api/nim/clients/define", methods=["POST"])
def nim_define_client():
    """Define an existing AIX client on a NIM master and initialise nimsh.

    This intentionally changes the remote NIM master and client. Client SSH
    credentials are request-only and are not stored in the local NIM profile.
    """
    data = request.get_json(force=True) or {}
    server_id = (data.get("server_id") or "").strip()
    srv = nim_store.get_server(server_id)
    if not srv:
        return jsonify({"ok": False, "error": "NIM server not found"}), 404

    try:
        client_name = _nim_safe_value(data.get("client_name"), "client name")
        client_host = _nim_safe_value(data.get("client_host"), "client host")
        network = _nim_safe_value(data.get("network"), "NIM network")
        adapter = _nim_safe_value(data.get("adapter"), "network adapter")
        platform = _nim_safe_value(data.get("platform"), "platform")
        kernel = _nim_safe_value(data.get("kernel"), "netboot kernel")
        mac = re.sub(r"[^0-9A-Fa-f]", "", data.get("mac") or "").upper()
        if not _NIM_MAC_ADDRESS.fullmatch(mac):
            raise ValueError("Invalid MAC address")
        client_port = int(data.get("client_port") or 22)
        if not 1 <= client_port <= 65535:
            raise ValueError("Invalid client SSH port")
    except (TypeError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    client_user = (data.get("client_username") or "root").strip()
    if not client_user:
        return jsonify({"ok": False, "error": "Client SSH username is required"}), 400

    master_name_result = _nim_run_server_command(srv, "hostname", timeout=30)
    if not master_name_result.get("ok"):
        return jsonify({"ok": False, "error": "Could not determine NIM master hostname"}), 502
    try:
        master_name = _nim_safe_value(master_name_result.get("output", "").splitlines()[0], "NIM master hostname")
    except (IndexError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502
    client_kwargs = dict(
        host=client_host, port=client_port, username=client_user,
        key_path=(data.get("client_key_path") or "").strip() or None,
        password=data.get("client_password") or None, timeout=30,
    )
    # Never create or reuse a master object when the client cannot run
    # niminit, or when its existing configuration would make niminit fail.
    prerequisites = _nim_client_prerequisites(client_kwargs)
    prerequisite_result = prerequisites["command_result"]
    if not prerequisite_result.get("ok"):
        return jsonify({"ok": False, "error": "Could not verify NIM prerequisites on the AIX client; no NIM master changes were made."}), 502
    if not prerequisites["niminit"] or not prerequisites["nim_client_fileset"]:
        return jsonify({"ok": False, "error": "The AIX client is missing niminit or the committed bos.sysmgt.nim.client fileset; no NIM master changes were made."}), 409
    if not prerequisites["niminfo_absent"]:
        return jsonify({"ok": False, "error": "The AIX client already has /etc/niminfo. Remove it first (rm -f /etc/niminfo), then run the wizard again; no NIM master changes were made."}), 409
    network_exists = _nim_run_server_command(srv, f"lsnim -l {shlex.quote(network)}", timeout=30)
    if not network_exists.get("ok"):
        return jsonify({"ok": False, "error": f"Selected NIM network '{network}' no longer exists; no NIM master changes were made."}), 409
    hosts_updates = []
    for target, command, entry in (
        ("NIM master", _nim_hosts_lookup_command(client_host, client_name), f"{client_host} {client_name}"),
        ("AIX client", _nim_hosts_lookup_command(srv["host"], master_name), f"{srv['host']} {master_name}"),
    ):
        lookup = (_nim_run_server_command(srv, command, timeout=30) if target == "NIM master"
                  else ssh_manager.run_command(command=command, **client_kwargs))
        if not lookup.get("ok"):
            return jsonify({"ok": False, "error": f"Could not read /etc/hosts on {target}"}), 502
        if not _nim_remote_hosts_contains(lookup, "", ""):
            if not data.get("add_hosts_entries"):
                return jsonify({"ok": False, "error": "Required /etc/hosts entries are missing. Confirm the Hosts step before executing."}), 409
            append_command = f"printf '%s\\n' {shlex.quote(entry)} >> /etc/hosts"
            update = (_nim_run_server_command(srv, append_command, timeout=30) if target == "NIM master"
                      else ssh_manager.run_command(command=append_command, **client_kwargs))
            hosts_updates.append({"name": f"Add /etc/hosts entry on {target}", "command": append_command,
                                  "ok": update.get("ok", False), "output": update.get("output", ""),
                                  "stderr": update.get("stderr", "") or update.get("error", "")})
            if not update.get("ok"):
                return jsonify({"ok": False, "error": f"Could not add /etc/hosts entry on {target}", "steps": hosts_updates}), 502

    # A previous request can successfully define the master object but fail
    # before niminit completes. Reuse that object so the user can safely retry
    # client initialisation after fixing DNS, routing, or /etc/hosts.
    exists = _nim_run_server_command(srv, f"lsnim {shlex.quote(client_name)}", timeout=30)
    steps = hosts_updates
    if exists.get("ok"):
        steps.append({
            "name": "Define client on NIM master",
            "command": f"lsnim {client_name}",
            "ok": True,
            "output": "Client already exists on the NIM master; reusing it for niminit retry.",
            "stderr": "",
        })
    else:
        # Define first: niminit needs the master to recognise the client object.
        if1 = f"{network} {client_host} {mac} {adapter}"
        define_command = (
            "nim -o define -t standalone "
            f"-a platform={shlex.quote(platform)} "
            f"-a netboot_kernel={shlex.quote(kernel)} "
            f"-a if1={shlex.quote(if1)} "
            "-a cable_type1=N/A -a connect=nimsh "
            f"{shlex.quote(client_name)}"
        )
        define_result = _nim_run_server_command(srv, define_command, timeout=60)
        steps.append({
            "name": "Define client on NIM master",
            "command": define_command,
            "ok": define_result.get("ok", False),
            "output": define_result.get("output", ""),
            "stderr": define_result.get("stderr", "") or define_result.get("error", ""),
        })
        if not define_result.get("ok"):
            return jsonify({"ok": False, "error": "NIM master could not define the client", "steps": steps}), 502

    niminit_command = (
        f"niminit -a name={shlex.quote(client_name)} "
        f"-a master={shlex.quote(srv['host'])} -a connect=nimsh"
    )
    init_result = ssh_manager.run_command(
        host=client_host, port=client_port, username=client_user,
        key_path=(data.get("client_key_path") or "").strip() or None,
        password=data.get("client_password") or None,
        command=niminit_command, timeout=60,
    )
    steps.append({
        "name": "Initialize NIMSH on client",
        "command": niminit_command,
        "ok": init_result.get("ok", False),
        "output": init_result.get("output", ""),
        "stderr": init_result.get("stderr", "") or init_result.get("error", ""),
    })
    if not init_result.get("ok"):
        return jsonify({
            "ok": False,
            "error": "Client was defined on the NIM master, but niminit failed on the client. Resolve the error then run niminit manually.",
            "steps": steps,
        }), 502

    verify_result = _nim_run_server_command(srv, f"lsnim -l {shlex.quote(client_name)}", timeout=30)
    steps.append({
        "name": "Verify client on NIM master",
        "command": f"lsnim -l {client_name}",
        "ok": verify_result.get("ok", False),
        "output": verify_result.get("output", ""),
        "stderr": verify_result.get("stderr", "") or verify_result.get("error", ""),
    })
    if not verify_result.get("ok"):
        return jsonify({"ok": False, "error": "Client was added but master verification failed", "steps": steps}), 502

    return jsonify({"ok": True, "steps": steps}), 201


# ── NIM Resources (read from server via SSH) ──────────────

@app.route("/api/nim/servers/<server_id>/resources", methods=["GET"])
def nim_server_resources(server_id):
    """Return the full NIM environment parsed from `lsnim -l`.

    Categorised into: master, spot, lpp_source, mksysb,
    standalone (clients), networks, and other.
    Optional query param ?type= filters to a single category.
    """
    srv = nim_store.get_server(server_id)
    if not srv:
        return jsonify({"ok": False, "error": "NIM server not found"}), 404

    cmd = "lsnim -l"
    result = ssh_manager.run_command(
        host=srv["host"],
        port=int(srv.get("ssh_port", 22)),
        username=srv.get("username", "root"),
        key_path=srv.get("key_path") or None,
        password=srv.get("password") or None,
        command=cmd,
        timeout=60,
    )

    # lsnim may exit non-zero but still produce valid output — accept it
    output = result.get("output", "")
    if not output and not result.get("ok"):
        err = result.get("error") or result.get("stderr") or "SSH command failed"
        logger.error("NIM lsnim -l failed: %s", err)
        return jsonify({"ok": False, "error": str(err)})

    objects = _parse_lsnim_stanzas(output)

    categories = {
        "master": [], "spot": [], "lpp_source": [], "mksysb": [],
        "standalone": [], "networks": [], "other": [],
    }
    for obj in objects:
        otype = obj.get("type", "")
        oclass = obj.get("class", "")
        if otype == "master":
            categories["master"].append(obj)
        elif otype == "spot":
            categories["spot"].append(obj)
        elif otype == "lpp_source":
            categories["lpp_source"].append(obj)
        elif otype == "mksysb":
            categories["mksysb"].append(obj)
        elif otype == "standalone":
            categories["standalone"].append(obj)
        elif oclass == "networks" or otype == "ent":
            categories["networks"].append(obj)
        else:
            categories["other"].append(obj)

    res_type = request.args.get("type", "").strip().lower()
    if res_type and res_type in categories:
        categories = {res_type: categories[res_type]}

    return jsonify({"ok": True, "data": categories})


@app.route("/api/nim/servers/<server_id>/resources/<path:resource_name>",
           methods=["GET"])
def nim_resource_detail(server_id, resource_name):
    """Show details for a single NIM resource (lsnim -l <name>)."""
    srv = nim_store.get_server(server_id)
    if not srv:
        return jsonify({"ok": False, "error": "NIM server not found"}), 404

    cmd = f"lsnim -l {resource_name}"
    result = ssh_manager.run_command(
        host=srv["host"],
        port=int(srv.get("ssh_port", 22)),
        username=srv.get("username", "root"),
        key_path=srv.get("key_path") or None,
        password=srv.get("password") or None,
        command=cmd,
        timeout=30,
    )

    output = result.get("output", "")
    if not output and not result.get("ok"):
        return jsonify({"ok": False, "error": result.get("error") or result.get("stderr") or "SSH failed"})

    objects = _parse_lsnim_stanzas(output)
    if objects:
        return jsonify({"ok": True, "data": objects[0]})
    return jsonify({"ok": True, "data": {"name": resource_name, "attrs": {}}})


@app.route("/api/nim/servers/<server_id>/nim-clients", methods=["GET"])
def nim_server_nim_clients(server_id):
    """List NIM client machines defined on the NIM server."""
    srv = nim_store.get_server(server_id)
    if not srv:
        return jsonify({"ok": False, "error": "NIM server not found"}), 404

    cmd = "lsnim -l -t standalone"
    result = ssh_manager.run_command(
        host=srv["host"],
        port=int(srv.get("ssh_port", 22)),
        username=srv.get("username", "root"),
        key_path=srv.get("key_path") or None,
        password=srv.get("password") or None,
        command=cmd,
        timeout=30,
    )

    output = result.get("output", "")
    if not output and not result.get("ok"):
        return jsonify({"ok": False, "error": result.get("error") or result.get("stderr") or "SSH failed"})

    clients = _parse_lsnim_stanzas(output)
    return jsonify({"ok": True, "data": clients})


@app.route("/api/nim/servers/<server_id>/standalone-clients/<client_name>", methods=["DELETE"])
def nim_remove_standalone_client(server_id, client_name):
    """Remove an existing standalone object directly from the NIM master."""
    srv = nim_store.get_server(server_id)
    if not srv:
        return jsonify({"ok": False, "error": "NIM server not found"}), 404
    try:
        client_name = _nim_safe_value(client_name, "NIM client name")
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    command = f"nim -o remove {shlex.quote(client_name)}"
    result = _nim_run_server_command(srv, command, timeout=60)
    if not result.get("ok"):
        return jsonify({
            "ok": False,
            "error": result.get("stderr") or result.get("error") or "NIM master could not remove the client object",
            "command": command,
        }), 502
    return jsonify({"ok": True, "command": command, "output": result.get("output", "")})


# ── NIM output parsers ───────────────────────────────────

def _parse_lsnim_stanzas(text: str) -> list:
    """Parse multi-stanza `lsnim -l` output.

    Each NIM object is a stanza:
        object_name:
           key   = value
           key   = value
    Repeated keys are collected into lists.
    """
    objects = []
    current_name = None
    current_attrs: dict = {}

    def _flush():
        if current_name is None:
            return
        obj = {"name": current_name, "attrs": current_attrs}
        for key in ("class", "type", "Rstate", "Mstate", "Cstate",
                     "location", "version", "release", "mod",
                     "oslevel_r", "oslevel_s", "server",
                     "alloc_count", "date_updated", "platform",
                     "netboot_kernel", "connect", "cpuid",
                     "net_addr", "snm", "Nstate", "arch",
                     "bos_license", "simages", "comments",
                     "prev_state", "Cstate_result", "Rstate_result"):
            val = current_attrs.get(key)
            if val is not None:
                obj[key] = val
        objects.append(obj)

    for line in text.splitlines():
        if line and not line[0].isspace() and line.rstrip().endswith(":"):
            _flush()
            current_name = line.rstrip().rstrip(":")
            current_attrs = {}
            continue
        stripped = line.strip()
        if "=" in stripped and current_name is not None:
            key, _, val = stripped.partition("=")
            key = key.strip()
            val = val.strip()
            if key in current_attrs:
                existing = current_attrs[key]
                if isinstance(existing, list):
                    existing.append(val)
                else:
                    current_attrs[key] = [existing, val]
            else:
                current_attrs[key] = val

    _flush()
    return objects


# ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5001, debug=True)

