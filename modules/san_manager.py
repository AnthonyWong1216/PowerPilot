"""
SAN Manager — vendor-aware, read-only fabric inspection for SAN switches.

Currently supports Brocade FOS and Cisco MDS/NX-OS. Reuses the existing
SSHManager to run CLI commands over SSH, then parses the raw output into
structured aliases / zones / zonesets for the UI.

This module is intentionally READ-ONLY for now (list operations only).
Editing (alicreate/zonecreate/cfgsave/etc.) can be layered on later using
the same command-builder + run_hmc_command pattern.
"""

import re
import logging
from typing import Dict, List

logger = logging.getLogger(__name__)


class SANManager:
    def __init__(self, ssh_manager):
        # Reuse the shared SSHManager (run_hmc_command works for any SSH host)
        self.ssh = ssh_manager

    # ──────────────────────────────────────────────────────
    # Public entry point
    # ──────────────────────────────────────────────────────

    def fetch_zoning(self, switch: dict, debug: bool = False) -> dict:
        """Return {ok, aliases, zones, zonesets, active_zoneset, raw?, error?}."""
        vendor = (switch.get("vendor") or "brocade").lower()
        if vendor == "cisco":
            return self._fetch_cisco(switch, debug)
        return self._fetch_brocade(switch, debug)

    def fetch_ports(self, switch: dict, debug: bool = False) -> dict:
        """Return {ok, switch_name, ports:[{index,state,status,speed,wwn,type}], raw?}."""
        vendor = (switch.get("vendor") or "brocade").lower()
        if vendor == "cisco":
            return self._fetch_ports_cisco(switch, debug)
        return self._fetch_ports_brocade(switch, debug)

    def fetch_port_login(self, switch: dict, port_index: int) -> dict:
        """Run portloginshow <port> and return parsed login entries."""
        vendor = (switch.get("vendor") or "brocade").lower()
        if vendor == "cisco":
            return self._port_login_cisco(switch, port_index)
        return self._port_login_brocade(switch, port_index)

    def _port_login_brocade(self, switch: dict, port_index: int) -> dict:
        """Run portloginshow <port> and parse the WWPN login table."""
        cmd = f"portloginshow {port_index}"
        r = self._run(switch, cmd)
        out = (r.get("output") or r.get("stderr") or "").strip()
        if not r.get("ok") and not out:
            return {"ok": False, "error": r.get("error", "portloginshow failed"),
                    "port": port_index, "logins": [], "raw": ""}
        logins = self._parse_portloginshow(out)
        return {"ok": True, "port": port_index, "logins": logins,
                "raw": out, "command": cmd}

    def _port_login_cisco(self, switch: dict, port_index: int) -> dict:
        """Cisco: show flogi database interface fc<x> equivalent."""
        # Cisco port index is typically like fc1/port_index — try fc1/N
        cmd = f"show flogi database"
        r = self._run(switch, cmd)
        out = (r.get("output") or "").strip()
        logins = []
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 4 and re.search(r'[0-9a-fA-F:]{23}', line):
                logins.append({"type": "Nx", "wwpn": parts[3] if len(parts) > 3 else "",
                               "wwnn": parts[2] if len(parts) > 2 else "",
                               "pid": parts[1] if len(parts) > 1 else ""})
        return {"ok": True, "port": port_index, "logins": logins,
                "raw": out, "command": cmd}

    @staticmethod
    def _parse_portloginshow(text: str) -> list:
        """Parse portloginshow output into a list of login dicts.

        Brocade FOS portloginshow output:
          Type PID        World Wide Name          credit df_sz cos
         ====  ========   ========================  ======  =====  ===
           N  021500;  10:00:00:b3:ff:af:0e:73;  060;  02048;  c;
           NL  021500;  10:00:00:b3:ff:af:0e:74;  060;  02048;  c;

        Also handles newer FOS which may output slightly differently.
        """
        logins = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith(("Type", "===", "type")):
                continue
            # WWN/WWPN pattern: xx:xx:xx:xx:xx:xx:xx:xx (with optional trailing semicolon)
            wwn_match = re.findall(r'[0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){7}', stripped)
            if not wwn_match:
                continue
            parts = re.split(r'[;\s]+', stripped)
            parts = [p.strip() for p in parts if p.strip()]
            entry_type = parts[0] if parts else "N"
            pid = ""
            # PID looks like 6 hex digits
            pid_match = re.search(r'\b([0-9a-fA-F]{6})\b', stripped)
            if pid_match:
                pid = pid_match.group(1)
            logins.append({
                "type": entry_type,
                "wwpn": wwn_match[0] if len(wwn_match) > 0 else "",
                "wwnn": wwn_match[1] if len(wwn_match) > 1 else "",
                "pid":  pid,
            })
        return logins

    # ──────────────────────────────────────────────────────
    # Write operations (alias / zone / zoneset management)
    # ──────────────────────────────────────────────────────

    def create_alias(self, switch: dict, alias_name: str, members: list) -> dict:
        """Create a new alias (alicreate / device-alias).
        members is a list of WWPN strings."""
        vendor = (switch.get("vendor") or "brocade").lower()
        if vendor == "cisco":
            return self._write_cisco_alias(switch, alias_name, members)
        return self._write_brocade_alias(switch, alias_name, members)

    def create_zone(self, switch: dict, zone_name: str, members: list) -> dict:
        """Create a new zone with the given members (alias names or WWPNs).
        Does NOT activate/save the zone config — caller must do that."""
        vendor = (switch.get("vendor") or "brocade").lower()
        if vendor == "cisco":
            return self._write_cisco_zone(switch, zone_name, members)
        return self._write_brocade_zone(switch, zone_name, members)

    def add_zone_to_zoneset(self, switch: dict, zoneset_name: str, zone_names: list) -> dict:
        """Add one or more zones to a zoneset (cfgadd / zoneset name ... + member zone)."""
        vendor = (switch.get("vendor") or "brocade").lower()
        if vendor == "cisco":
            return self._write_cisco_zoneset_add(switch, zoneset_name, zone_names)
        return self._write_brocade_zoneset_add(switch, zoneset_name, zone_names)

    def activate_zoneset(self, switch: dict, zoneset_name: str) -> dict:
        """Activate (enable) a zoneset / cfg."""
        vendor = (switch.get("vendor") or "brocade").lower()
        if vendor == "cisco":
            return self._write_cisco_activate(switch, zoneset_name)
        return self._write_brocade_activate(switch, zoneset_name)

    def delete_alias(self, switch: dict, alias_name: str) -> dict:
        """Delete an alias (alidelete / device-alias remove)."""
        vendor = (switch.get("vendor") or "brocade").lower()
        if vendor == "cisco":
            return self._delete_cisco_alias(switch, alias_name)
        return self._delete_brocade_alias(switch, alias_name)

    def delete_zone(self, switch: dict, zone_name: str) -> dict:
        """Delete a zone (zonedelete)."""
        vendor = (switch.get("vendor") or "brocade").lower()
        if vendor == "cisco":
            return self._delete_cisco_zone(switch, zone_name)
        return self._delete_brocade_zone(switch, zone_name)

    # ── Brocade model / FOS flavor helpers ─────────────────

    # Legacy Brocade models (FOS 6.x / 7.x) that do NOT support the
    # newer non-interactive flags such as `cfgsave -f` or `cfgshow --all`.
    # The IBM/Brocade 2498-24B (SAN24B-4) falls into this group.
    _LEGACY_MODEL_HINTS = (
        "24b", "2498", "san24b", "300", "5000", "5100", "5300",
        "silkworm", "fos6", "fos7",
    )

    def _is_legacy_brocade(self, switch: dict) -> bool:
        """Return True when the switch should use the legacy (interactive)
        FOS command variants.

        Resolution order:
          1. explicit model == "legacy"  → True
          2. explicit model == "modern"  → False
          3. model == "auto"/blank       → sniff the model/name string for
             known legacy hints (e.g. "24B", "2498").
        """
        model = (switch.get("model") or "auto").strip().lower()
        if model == "legacy":
            return True
        if model == "modern":
            return False
        # auto-detect: look at model + name for known legacy identifiers
        haystack = " ".join([
            str(switch.get("model") or ""),
            str(switch.get("name") or ""),
            str(switch.get("notes") or ""),
        ]).lower()
        return any(h in haystack for h in self._LEGACY_MODEL_HINTS)

    def _cfgsave_spec(self, switch: dict):
        """Return the cfgsave command spec appropriate for the switch model.

        Modern FOS supports `cfgsave -f` (non-interactive). Legacy FOS
        (e.g. 24B / FOS 6-7.x) has no `-f` flag and prompts for
        confirmation, so we send the command with 'y' piped to stdin.
        """
        if self._is_legacy_brocade(switch):
            return ("cfgsave", "y")   # (command, stdin_input)
        return "cfgsave -f"

    @staticmethod
    def _cfgsave_key(spec) -> str:
        """The cmd_map key / display name for the cfgsave step."""
        cmd = spec[0] if isinstance(spec, tuple) else spec
        return cmd  # "cfgsave" or "cfgsave -f"

    def _cfgshow_cmd(self, switch: dict) -> str:
        """`cfgshow --all` on modern FOS, plain `cfgshow` on legacy models."""
        if self._is_legacy_brocade(switch):
            return "cfgshow"
        return "cfgshow --all 2>/dev/null || cfgshow"

    # ── Brocade write helpers ──────────────────────────────

    def _brocade_batch(self, switch: dict, cmds: dict) -> dict:

        """Run commands on ONE SSH connection. cmds is ordered {name: cmd_or_(cmd,stdin)}."""
        return self.ssh.run_hmc_commands_batch(
            host=switch["host"],
            port=int(switch.get("ssh_port", 22)),
            username=switch.get("username", "admin"),
            key_path=switch.get("key_path") or None,
            password=switch.get("password") or None,
            commands=cmds,
        )

    def _make_commands_run(self, raw: dict, cmd_map: dict) -> list:
        """Convert batch result dict into commands_run list for UI display."""
        result = []
        for name, cmd_spec in cmd_map.items():
            cmd = cmd_spec[0] if isinstance(cmd_spec, tuple) else cmd_spec
            r   = raw.get(name, {})
            out = (r.get("output") or r.get("stderr") or "").strip()
            ok  = bool(r.get("ok"))
            result.append({
                "name":    name,
                "command": cmd,
                "output":  out or "(no output)",
                "ok":      ok,
                "error":   r.get("error", "") if not ok else "",
            })
        return result

    def _write_brocade_alias(self, switch: dict, alias_name: str, members: list) -> dict:
        """cfgabort → alicreate → alishow verify → cfgsave -f → cfgenable → cfgshow verify
        All on ONE SSH connection to hold the zone transaction lock."""
        members_str = ";".join(m.strip() for m in members if m.strip())
        ali_cmd = f'alicreate "{alias_name}", "{members_str}"'
        logger.info("[SAN] alias batch on %s: %r", switch.get("host"), ali_cmd)

        # Get active zoneset first (separate quick connection)
        actv_r     = self._run(switch, "cfgactvshow")
        active_cfg = self._parse_brocade_active(actv_r.get("output", ""))

        # Build the ordered command sequence on ONE connection.
        # cfgsave variant depends on model (modern: `cfgsave -f`, legacy 24B: interactive `cfgsave` + 'y').
        cfgsave_spec = self._cfgsave_spec(switch)
        cfgsave_key  = self._cfgsave_key(cfgsave_spec)
        cmd_map = {
            "cfgabort":         "cfgabort",
            "alicreate":        ali_cmd,
            "alishow (verify)": f'alishow "{alias_name}"',
            cfgsave_key:        cfgsave_spec,
        }
        if active_cfg:
            cmd_map[f"cfgenable ({active_cfg})"] = (f'cfgenable "{active_cfg}"', "y")
        cmd_map["cfgshow (verify)"] = "cfgshow"


        raw = self._brocade_batch(switch, cmd_map)
        commands_run = self._make_commands_run(raw, cmd_map)

        # Evaluate results
        ali_r   = raw.get("alicreate", {})
        ali_out = (ali_r.get("output") or ali_r.get("stderr") or ali_r.get("error") or "").strip()
        ali_combined = ali_out.lower()
        ali_failed = any(kw in ali_combined for kw in (
            "invalid", "already exists", "name exists", "not permitted", "fail"
        )) if ali_combined else False

        if ali_failed:
            return {"ok": False, "error": ali_out or "alicreate failed",
                    "commands_run": commands_run}

        show_out       = (raw.get("alishow (verify)", {}).get("output") or "").strip()
        alias_verified = alias_name.lower() in show_out.lower()
        if not alias_verified:
            return {"ok": False,
                    "error": f"Alias '{alias_name}' not found after alicreate. Check WWPN format and uniqueness.",
                    "commands_run": commands_run}

        cfg_r    = raw.get(cfgsave_key, {})
        cfg_out  = (cfg_r.get("output") or cfg_r.get("stderr") or "").strip()
        cfg_text = (cfg_out + " " + cfg_r.get("error", "")).lower()
        cfg_ok   = bool(cfg_r.get("ok")) or any(kw in cfg_text for kw in ("updating","done","saved","commit","nothing"))

        if not cfg_ok:
            return {"ok": False, "error": f"{cfgsave_key} failed: {cfg_out}",
                    "commands_run": commands_run}


        verify_out = (raw.get("cfgshow (verify)", {}).get("output") or "").strip()
        in_cfgshow = alias_name.lower() in verify_out.lower()
        nothing_changed = "nothing" in cfg_text and ("changed" in cfg_text or "save" in cfg_text)

        ena_msg = f"and zoneset '{active_cfg}' re-activated." if active_cfg else "(no active zoneset)"
        success_msg = (
            f"Alias '{alias_name}' already in flash, {ena_msg}"
            if nothing_changed else
            f"Alias '{alias_name}' created and saved, {ena_msg}"
        )
        if not in_cfgshow:
            success_msg += " ⚠ Not yet visible in cfgshow."
        return {"ok": True, "output": success_msg, "commands_run": commands_run}

    def _write_brocade_zone(self, switch: dict, zone_name: str, members: list) -> dict:
        """cfgabort → zonecreate → cfgsave -f → cfgenable — all on ONE connection."""
        members_str = ";".join(m.strip() for m in members if m.strip())
        actv_r     = self._run(switch, "cfgactvshow")
        active_cfg = self._parse_brocade_active(actv_r.get("output", ""))

        cfgsave_spec = self._cfgsave_spec(switch)
        cfgsave_key  = self._cfgsave_key(cfgsave_spec)
        cmd_map = {
            "cfgabort":    "cfgabort",
            "zonecreate":  f'zonecreate "{zone_name}", "{members_str}"',
            cfgsave_key:   cfgsave_spec,
        }
        if active_cfg:
            cmd_map[f"cfgenable ({active_cfg})"] = (f'cfgenable "{active_cfg}"', "y")

        raw = self._brocade_batch(switch, cmd_map)
        commands_run = self._make_commands_run(raw, cmd_map)

        zr  = raw.get("zonecreate", {})
        z_out = (zr.get("output") or zr.get("stderr") or zr.get("error") or "").strip()
        if any(kw in z_out.lower() for kw in ("error", "invalid", "already", "fail", "not permitted")):
            return {"ok": False, "error": z_out or "zonecreate failed", "commands_run": commands_run}

        cfg_r   = raw.get(cfgsave_key, {})
        cfg_out = (cfg_r.get("output") or cfg_r.get("stderr") or "").strip()
        cfg_text = (cfg_out + " " + cfg_r.get("error","")).lower()
        cfg_ok = bool(cfg_r.get("ok")) or any(kw in cfg_text for kw in ("updating","done","saved","commit","nothing"))
        if not cfg_ok:
            return {"ok": False, "error": f"{cfgsave_key} failed: {cfg_out}", "commands_run": commands_run}

        return {"ok": True, "output": f"Zone '{zone_name}' created and saved.", "commands_run": commands_run}


    def _write_brocade_zoneset_add(self, switch: dict, zoneset_name: str, zone_names: list) -> dict:
        """cfgabort → cfgadd → cfgsave -f → cfgenable — all on ONE connection."""
        zones_str  = ";".join(z.strip() for z in zone_names if z.strip())
        actv_r     = self._run(switch, "cfgactvshow")
        active_cfg = self._parse_brocade_active(actv_r.get("output", ""))

        cfgsave_spec = self._cfgsave_spec(switch)
        cfgsave_key  = self._cfgsave_key(cfgsave_spec)
        cmd_map = {
            "cfgabort":  "cfgabort",
            "cfgadd":    f'cfgadd "{zoneset_name}", "{zones_str}"',
            cfgsave_key: cfgsave_spec,
        }
        if active_cfg:
            cmd_map[f"cfgenable ({active_cfg})"] = (f'cfgenable "{active_cfg}"', "y")

        raw = self._brocade_batch(switch, cmd_map)
        commands_run = self._make_commands_run(raw, cmd_map)

        ar  = raw.get("cfgadd", {})
        a_out = (ar.get("output") or ar.get("stderr") or ar.get("error") or "").strip()
        if any(kw in a_out.lower() for kw in ("error", "invalid", "fail", "not permitted")):
            return {"ok": False, "error": a_out or "cfgadd failed", "commands_run": commands_run}

        cfg_r   = raw.get(cfgsave_key, {})
        cfg_out = (cfg_r.get("output") or cfg_r.get("stderr") or "").strip()
        cfg_text = (cfg_out + " " + cfg_r.get("error","")).lower()
        cfg_ok = bool(cfg_r.get("ok")) or any(kw in cfg_text for kw in ("updating","done","saved","commit","nothing"))
        if not cfg_ok:
            return {"ok": False, "error": f"{cfgsave_key} failed: {cfg_out}", "commands_run": commands_run}

        return {"ok": True, "output": f"Zones added to '{zoneset_name}' and saved.", "commands_run": commands_run}


    def _delete_brocade_alias(self, switch: dict, alias_name: str) -> dict:
        """cfgabort → alidelete → cfgsave -f → cfgenable — on ONE connection."""
        actv_r     = self._run(switch, "cfgactvshow")
        active_cfg = self._parse_brocade_active(actv_r.get("output", ""))
        cfgsave_spec = self._cfgsave_spec(switch)
        cfgsave_key  = self._cfgsave_key(cfgsave_spec)
        cmd_map = {
            "cfgabort":   "cfgabort",
            "alidelete":  f'alidelete "{alias_name}"',
            cfgsave_key:  cfgsave_spec,
        }
        if active_cfg:
            cmd_map[f"cfgenable ({active_cfg})"] = (f'cfgenable "{active_cfg}"', "y")
        raw = self._brocade_batch(switch, cmd_map)
        commands_run = self._make_commands_run(raw, cmd_map)
        dr  = raw.get("alidelete", {})
        d_out = (dr.get("output") or dr.get("stderr") or dr.get("error") or "").strip()
        if any(kw in d_out.lower() for kw in ("error", "invalid", "not found", "fail", "not permitted")):
            return {"ok": False, "error": d_out or "alidelete failed", "commands_run": commands_run}
        cfg_r   = raw.get(cfgsave_key, {})
        cfg_out = (cfg_r.get("output") or cfg_r.get("stderr") or "").strip()
        cfg_text = (cfg_out + " " + cfg_r.get("error","")).lower()
        cfg_ok = bool(cfg_r.get("ok")) or any(kw in cfg_text for kw in ("updating","done","saved","commit","nothing"))
        if not cfg_ok:
            return {"ok": False, "error": f"{cfgsave_key} failed: {cfg_out}", "commands_run": commands_run}
        return {"ok": True, "output": f"Alias '{alias_name}' deleted and saved.", "commands_run": commands_run}


    def _delete_brocade_zone(self, switch: dict, zone_name: str) -> dict:
        """cfgabort → zonedelete → cfgsave -f → cfgenable — on ONE connection."""
        actv_r     = self._run(switch, "cfgactvshow")
        active_cfg = self._parse_brocade_active(actv_r.get("output", ""))
        cfgsave_spec = self._cfgsave_spec(switch)
        cfgsave_key  = self._cfgsave_key(cfgsave_spec)
        cmd_map = {
            "cfgabort":    "cfgabort",
            "zonedelete":  f'zonedelete "{zone_name}"',
            cfgsave_key:   cfgsave_spec,
        }
        if active_cfg:
            cmd_map[f"cfgenable ({active_cfg})"] = (f'cfgenable "{active_cfg}"', "y")
        raw = self._brocade_batch(switch, cmd_map)
        commands_run = self._make_commands_run(raw, cmd_map)
        dr  = raw.get("zonedelete", {})
        d_out = (dr.get("output") or dr.get("stderr") or dr.get("error") or "").strip()
        if any(kw in d_out.lower() for kw in ("error", "invalid", "not found", "fail", "not permitted")):
            return {"ok": False, "error": d_out or "zonedelete failed", "commands_run": commands_run}
        cfg_r   = raw.get(cfgsave_key, {})
        cfg_out = (cfg_r.get("output") or cfg_r.get("stderr") or "").strip()
        cfg_text = (cfg_out + " " + cfg_r.get("error","")).lower()
        cfg_ok = bool(cfg_r.get("ok")) or any(kw in cfg_text for kw in ("updating","done","saved","commit","nothing"))
        if not cfg_ok:
            return {"ok": False, "error": f"{cfgsave_key} failed: {cfg_out}", "commands_run": commands_run}
        return {"ok": True, "output": f"Zone '{zone_name}' deleted and saved.", "commands_run": commands_run}


    def _delete_cisco_alias(self, switch: dict, alias_name: str) -> dict:
        lines = ["device-alias database", f"  no device-alias name {alias_name}", "device-alias commit"]
        return self._run_cisco_config(switch, lines)

    def _delete_cisco_zone(self, switch: dict, zone_name: str) -> dict:
        lines = [f"no zone name {zone_name} vsan 1"]
        return self._run_cisco_config(switch, lines)

    def _write_brocade_activate(self, switch: dict, zoneset_name: str) -> dict:
        """cfgenable "cfg_name" to activate a Brocade zoneset.
        cfgenable prompts 'Do you want to enable ... (yes, y, no, n)' — auto-answers 'y'.
        """
        ena_r   = self._run(switch, f'cfgenable "{zoneset_name}"', stdin_input="y")
        ena_out = (ena_r.get("output") or ena_r.get("stderr") or "").strip()
        ena_ok  = bool(ena_r.get("ok"))
        if not ena_ok:
            ena_text = (ena_out + " " + ena_r.get("error", "")).lower()
            if any(kw in ena_text for kw in ("enabled", "done", "active")):
                ena_ok = True
        commands_run = [
            {"name": "cfgenable", "command": f'cfgenable "{zoneset_name}"',
             "output": ena_out or "(no output — success)", "ok": ena_ok,
             "error": ena_r.get("error", "") if not ena_ok else ""},
        ]
        if not ena_ok:
            return {"ok": False, "error": ena_r.get("error", "cfgenable failed"),
                    "commands_run": commands_run}
        return {"ok": True, "output": f"Zoneset '{zoneset_name}' activated.",
                "commands_run": commands_run}

    # ── Brocade write helpers (end) ────────────────────────

    # ── Cisco write helpers ────────────────────────────────

    def _write_cisco_alias(self, switch: dict, alias_name: str, members: list) -> dict:
        """device-alias database + commit"""
        lines = ["device-alias database"]
        for m in members:
            m = m.strip()
            if m:
                lines.append(f"  device-alias name {alias_name} pwwn {m}")
        lines.append("device-alias commit")
        return self._run_cisco_config(switch, lines)

    def _write_cisco_zone(self, switch: dict, zone_name: str, members: list) -> dict:
        """zone name ... vsan 1 + member ... (VSAN 1 by default — members pass vsan if needed)"""
        # Detect VSAN from members if encoded as "wwpn@vsan"
        vsan = "1"
        clean_members = []
        for m in members:
            if "@" in m:
                parts = m.split("@", 1)
                clean_members.append(parts[0].strip())
                vsan = parts[1].strip()
            else:
                clean_members.append(m.strip())
        lines = [f"zone name {zone_name} vsan {vsan}"]
        for m in clean_members:
            if m:
                lines.append(f"  member pwwn {m}")
        lines.append("exit")
        return self._run_cisco_config(switch, lines)

    def _write_cisco_zoneset_add(self, switch: dict, zoneset_name: str, zone_names: list) -> dict:
        """zoneset name ... vsan 1 + member zone_name"""
        vsan = "1"
        lines = [f"zoneset name {zoneset_name} vsan {vsan}"]
        for z in zone_names:
            if z.strip():
                lines.append(f"  member {z.strip()}")
        lines.append("exit")
        return self._run_cisco_config(switch, lines)

    def _write_cisco_activate(self, switch: dict, zoneset_name: str) -> dict:
        """zoneset activate name ... vsan 1"""
        lines = [f"zoneset activate name {zoneset_name} vsan 1"]
        return self._run_cisco_config(switch, lines)

    def _run_cisco_config(self, switch: dict, config_lines: list) -> dict:
        """Send config-mode commands to a Cisco switch."""
        full = "configure terminal\n" + "\n".join(config_lines) + "\nend\ncopy running-config startup-config\n"
        result = self._run(switch, full)
        cmd_str = full.replace("\n", " ; ")
        if result.get("ok"):
            return {
                "ok":      True,
                "output":  result.get("output", "").strip(),
                "command": cmd_str,
            }
        return {
            "ok":      False,
            "error":   result.get("error", "Command failed"),
            "output":  result.get("output", "").strip(),
            "command": cmd_str,
        }

    def _run_write_batch(self, switch: dict, commands: dict) -> dict:
        """Run an ordered set of write commands and collect results.

        Each value in ``commands`` can be:
          - a plain string  → sent as the command, no stdin
          - a (cmd, stdin)  → cmd is sent; stdin string is piped to its stdin

        Always returns 'commands_run' (list of {name, cmd, output, ok})
        so the UI can show exactly what was sent and what came back.
        """
        combined_output = []
        commands_run = []
        for name, spec in commands.items():
            # Unpack spec: plain string or (cmd_string, stdin_input)
            if isinstance(spec, tuple):
                cmd, stdin_input = spec
            else:
                cmd, stdin_input = spec, None

            r = self._run(switch, cmd, stdin_input=stdin_input)
            step_out = (r.get("output") or r.get("stderr") or "").strip()
            step_ok  = bool(r.get("ok"))
            # Display the original cmd (without the echo y | prefix) for clarity
            display_cmd = cmd
            commands_run.append({
                "name":    name,
                "command": display_cmd,
                "output":  step_out,
                "ok":      step_ok,
                "error":   r.get("error", "") if not step_ok else "",
            })
            if step_ok and step_out:
                combined_output.append(f"[{name}] {step_out}")
            if not step_ok:
                return {
                    "ok":          False,
                    "error":       r.get("error", f"Command '{name}' failed"),
                    "output":      "\n".join(combined_output),
                    "failed_cmd":  name,
                    "commands_run": commands_run,
                }
        return {
            "ok":           True,
            "output":       "\n".join(combined_output) or "Done.",
            "commands_run": commands_run,
        }


    # ──────────────────────────────────────────────────────
    # SSH helper
    # ──────────────────────────────────────────────────────

    def _run(self, switch: dict, command: str, stdin_input=None) -> dict:
        return self.ssh.run_hmc_command(
            host=switch["host"],
            port=int(switch.get("ssh_port", 22)),
            username=switch.get("username", "admin"),
            key_path=switch.get("key_path") or None,
            password=switch.get("password") or None,
            command=command,
            stdin_input=stdin_input,
        )

    # ──────────────────────────────────────────────────────
    # Brocade FOS
    # ──────────────────────────────────────────────────────

    def _fetch_brocade(self, switch: dict, debug: bool) -> dict:
        # Run each command on its own SSH connection to avoid Brocade FOS
        # multi-exec issues, and use separate connections to prevent pager
        # truncation. We also send CTRL-Q / 'q' as stdin to skip any --More-- prompt.
        # On Brocade FOS, piping through a wide terminal avoids the pager entirely.
        # Legacy models (e.g. 24B) don't support `cfgshow --all`, so pick the
        # appropriate variant based on the switch model.
        cfg_r = self._run(switch, self._cfgshow_cmd(switch))
        actv_r = self._run(switch, "cfgactvshow")

        raw = {
            "cfgshow":     cfg_r,
            "cfgactvshow": actv_r,
        }

        cfg = cfg_r
        if not cfg.get("ok") and not cfg.get("output"):
            result = {"ok": False,
                      "error": cfg.get("error", "cfgshow returned no data")}
            if debug:
                result["raw"] = raw
            return result

        parsed = self._parse_brocade_cfgshow(cfg.get("output", ""))
        active = self._parse_brocade_active(actv_r.get("output", ""))
        parsed["active_zoneset"] = active or parsed.get("active_zoneset", "")
        parsed["ok"] = True
        if debug:
            parsed["raw"] = raw
        return parsed

    @staticmethod
    def _parse_brocade_cfgshow(text: str) -> dict:
        """Parse `cfgshow` output.

        Typical layout:
            Defined configuration:
             cfg:   PROD_CFG   zone1; zone2
             zone:  zone1      alias1; alias2
             alias: alias1     10:00:00:...
            Effective configuration:
             cfg:   PROD_CFG
             ...
        """
        aliases: List[Dict] = []
        zones: List[Dict] = []
        zonesets: List[Dict] = []
        active_zoneset = ""

        section = None          # "defined" | "effective"
        cur_kind = None         # "alias" | "zone" | "cfg"
        cur_name = None
        cur_members: List[str] = []

        def flush():
            nonlocal cur_kind, cur_name, cur_members
            if not cur_name:
                return
            members = [m.strip() for m in cur_members if m.strip()]
            if cur_kind == "alias":
                aliases.append({"name": cur_name, "members": members})
            elif cur_kind == "zone":
                zones.append({"name": cur_name, "members": members})
            elif cur_kind == "cfg":
                zonesets.append({"name": cur_name, "members": members})
            cur_kind = cur_name = None
            cur_members = []

        for line in text.splitlines():
            stripped = line.strip()
            low = stripped.lower()
            if low.startswith("defined configuration"):
                flush(); section = "defined"; continue
            if low.startswith("effective configuration"):
                flush(); section = "effective"; continue
            if not stripped:
                continue

            m = re.match(r"^(cfg|zone|alias):\s+(\S+)\s*(.*)$", stripped, re.IGNORECASE)
            if m:
                flush()
                kind = m.group(1).lower()
                name = m.group(2)
                rest = m.group(3)
                if section == "effective" and kind == "cfg":
                    active_zoneset = name
                    # effective section lists members too; skip re-adding cfg
                    cur_kind = None
                    continue
                cur_kind = kind
                cur_name = name
                cur_members = [x for x in re.split(r"[;\s]+", rest) if x]
            else:
                # continuation line — more members for the current object
                if cur_kind:
                    cur_members += [x for x in re.split(r"[;\s]+", stripped) if x]

        flush()
        return {
            "aliases": aliases,
            "zones": zones,
            "zonesets": zonesets,
            "active_zoneset": active_zoneset,
        }

    @staticmethod
    def _parse_brocade_active(text: str) -> str:
        """Extract the active cfg name from `cfgactvshow`."""
        for line in text.splitlines():
            m = re.search(r"cfg:\s+(\S+)", line, re.IGNORECASE)
            if m:
                return m.group(1)
        return ""

    # ──────────────────────────────────────────────────────
    # Cisco MDS / NX-OS
    # ──────────────────────────────────────────────────────

    def _fetch_cisco(self, switch: dict, debug: bool) -> dict:
        commands = {
            "device_alias": "show device-alias database",
            "zoneset":      "show zoneset",
            "zoneset_active": "show zoneset active",
        }
        raw = self.ssh.run_hmc_commands_batch(
            host=switch["host"],
            port=int(switch.get("ssh_port", 22)),
            username=switch.get("username", "admin"),
            key_path=switch.get("key_path") or None,
            password=switch.get("password") or None,
            commands=commands,
        )

        aliases = self._parse_cisco_aliases(raw.get("device_alias", {}).get("output", ""))
        zones, zonesets = self._parse_cisco_zoneset(raw.get("zoneset", {}).get("output", ""))
        active = self._parse_cisco_active(raw.get("zoneset_active", {}).get("output", ""))

        result = {
            "ok": True,
            "aliases": aliases,
            "zones": zones,
            "zonesets": zonesets,
            "active_zoneset": active,
        }
        if debug:
            result["raw"] = raw
        return result

    @staticmethod
    def _parse_cisco_aliases(text: str) -> List[Dict]:
        aliases = []
        for line in text.splitlines():
            m = re.search(r"device-alias name\s+(\S+)\s+pwwn\s+(\S+)", line, re.IGNORECASE)
            if m:
                aliases.append({"name": m.group(1), "members": [m.group(2)]})
        return aliases

    @staticmethod
    def _parse_cisco_zoneset(text: str) -> tuple:
        zones: List[Dict] = []
        zonesets: List[Dict] = []
        cur_set = None
        cur_zone = None
        for line in text.splitlines():
            stripped = line.strip()
            ms = re.match(r"zoneset name\s+(\S+)", stripped, re.IGNORECASE)
            if ms:
                cur_set = {"name": ms.group(1), "members": []}
                zonesets.append(cur_set)
                continue
            mz = re.match(r"zone name\s+(\S+)", stripped, re.IGNORECASE)
            if mz:
                cur_zone = {"name": mz.group(1), "members": []}
                zones.append(cur_zone)
                if cur_set is not None:
                    cur_set["members"].append(mz.group(1))
                continue
            mm = re.search(r"(?:pwwn|device-alias)\s+(\S+)", stripped, re.IGNORECASE)
            if mm and cur_zone is not None:
                cur_zone["members"].append(mm.group(1))
        return zones, zonesets

    @staticmethod
    def _parse_cisco_active(text: str) -> str:
        m = re.search(r"zoneset name\s+(\S+)", text, re.IGNORECASE)
        return m.group(1) if m else ""

    # ──────────────────────────────────────────────────────
    # Port status — Brocade (switchshow)
    # ──────────────────────────────────────────────────────

    def _fetch_ports_brocade(self, switch: dict, debug: bool) -> dict:
        raw = self.ssh.run_hmc_commands_batch(
            host=switch["host"],
            port=int(switch.get("ssh_port", 22)),
            username=switch.get("username", "admin"),
            key_path=switch.get("key_path") or None,
            password=switch.get("password") or None,
            commands={"switchshow": "switchshow"},
        )
        sw = raw.get("switchshow", {})
        if not sw.get("ok") and not sw.get("output"):
            result = {"ok": False, "error": sw.get("error", "switchshow returned no data")}
            if debug:
                result["raw"] = raw
            return result

        name, ports = self._parse_brocade_switchshow(sw.get("output", ""))
        result = {"ok": True, "switch_name": name or switch.get("name", ""),
                  "ports": ports}
        if debug:
            result["raw"] = raw
        return result

    @staticmethod
    def _parse_brocade_switchshow(text: str):
        """Parse `switchshow` port table.

        The port section looks like (columns vary slightly by FOS version):
            Index Port Address Media Speed State     Proto
            =============================================
              0   0   010000   id    N16   Online    FC  F-Port 10:00:...
              1   1   010100   id    --    No_Light   FC
        We extract index, speed, state and the trailing WWN/type if present.
        """
        name = ""
        ports = []
        in_table = False
        for line in text.splitlines():
            raw_line = line.rstrip()
            stripped = raw_line.strip()
            if not stripped:
                continue

            mn = re.match(r"switchName:\s*(\S+)", stripped, re.IGNORECASE)
            if mn:
                name = mn.group(1)
                continue

            low = stripped.lower()
            # Header row marks the start of the port table
            if low.startswith("index") and "state" in low:
                in_table = True
                continue
            if set(stripped) <= set("=-") and len(stripped) > 3:
                # separator line
                continue
            if not in_table:
                continue

            parts = stripped.split()
            if len(parts) < 2 or not parts[0].isdigit():
                continue

            index = parts[0]
            # State is the first token that looks like a known state keyword
            state = ""
            speed = ""
            for tok in parts[1:]:
                if re.match(r"^(Online|No_Light|No_Module|No_Sync|In_Sync|Laser_Flt|"
                            r"Port_Flt|Offline|Testing|Faulty|Mod_Val|No_SigDet|Disabled)$",
                            tok, re.IGNORECASE):
                    state = tok
                    break
            # Speed token looks like N16 / 16G / 32G / --
            for tok in parts[1:]:
                if re.match(r"^(N?\d+G?|--|AN|auto)$", tok, re.IGNORECASE) and tok not in (index,):
                    speed = tok
                    break

            # WWN if present anywhere on the line (Online F-Ports)
            wwn = ""
            mw = re.search(r"([0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){7})", stripped)
            if mw:
                wwn = mw.group(1)

            # Port type: F-Port / E-Port / etc.
            ptype = ""
            mt = re.search(r"\b([EFNGU]-?Port)\b", stripped, re.IGNORECASE)
            if mt:
                ptype = mt.group(1)

            up = bool(state) and state.lower() == "online"
            ports.append({
                "index":  index,
                "state":  state or "—",
                "speed":  speed or "—",
                "wwn":    wwn,
                "type":   ptype or "—",
                "up":     up,
            })
        return name, ports

    # ──────────────────────────────────────────────────────
    # Port status — Cisco (show interface brief)
    # ──────────────────────────────────────────────────────

    def _fetch_ports_cisco(self, switch: dict, debug: bool) -> dict:
        raw = self.ssh.run_hmc_commands_batch(
            host=switch["host"],
            port=int(switch.get("ssh_port", 22)),
            username=switch.get("username", "admin"),
            key_path=switch.get("key_path") or None,
            password=switch.get("password") or None,
            commands={"intf": "show interface brief"},
        )
        intf = raw.get("intf", {})
        if not intf.get("ok") and not intf.get("output"):
            result = {"ok": False, "error": intf.get("error", "show interface brief returned no data")}
            if debug:
                result["raw"] = raw
            return result

        ports = self._parse_cisco_intf_brief(intf.get("output", ""))
        result = {"ok": True, "switch_name": switch.get("name", ""), "ports": ports}
        if debug:
            result["raw"] = raw
        return result

    @staticmethod
    def _parse_cisco_intf_brief(text: str) -> List[Dict]:
        """Parse `show interface brief`.

        Rows look like:
            fc1/1   1   auto  on  up      swl   F  16    --
        Interface, VSAN, ..., Status column contains up/down/trunking/notConnected.
        """
        ports = []
        for line in text.splitlines():
            stripped = line.strip()
            m = re.match(r"^(fc\d+/\d+)\s+(.*)$", stripped, re.IGNORECASE)
            if not m:
                continue
            iface = m.group(1)
            rest = m.group(2).split()
            # Find the status token (up/down/trunking/notConnected/sfpAbsent/errDisabled)
            status = ""
            for tok in rest:
                if re.match(r"^(up|down|trunking|notConnected|sfpAbsent|errDisabled|"
                            r"init|offline|linkFailure)$", tok, re.IGNORECASE):
                    status = tok
                    break
            # Speed token like 16, 32, auto
            speed = ""
            for tok in rest:
                if re.match(r"^(\d+G?|auto)$", tok, re.IGNORECASE):
                    speed = tok
                    break
            # Port mode (F/E/TE/NP) — single letter tokens
            ptype = ""
            for tok in rest:
                if re.match(r"^(F|E|TE|NP|TF|SD|Fx)$", tok):
                    ptype = tok + "-Port"
                    break

            ports.append({
                "index":  iface,
                "state":  status or "—",
                "speed":  speed or "—",
                "wwn":    "",
                "type":   ptype or "—",
                "up":     status.lower() in ("up", "trunking"),
            })
        return ports

