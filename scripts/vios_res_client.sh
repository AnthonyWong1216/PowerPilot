#!/usr/bin/ksh
# Program name: vios_res_client
# Purpose: Client LPAR resilience test helper for Dual-VIOS failover testing.
#          Auto-discovers network (enX) and disk (hdiskX) devices, and captures
#          timestamped evidence logs BEFORE / DURING / AFTER a VIOS shutdown or restart.
#          Replaces manual command entry + screenshots.
# Disclaimer: Provided "as is". Use at your own risk. Test in non-prod first.
# Version: 1.3
#   1.3 - Added lscfg-based location code collection for fcsX and entX adapters and
#         derivation of the LPAR virtual adapter slot (-V<lparid> / -C<slot> / -T<port>).
#         New action: 'adapters'. Slot map also embedded in discover + every snapshot.

# License: MIT
# Author: AnthonyWong1216
#
# Conventions (same as lsseasV4):
#   f_function  = functions
#   v_variable  = variables
#   t_timeout   = timeout values
#
# Run this on the CLIENT LPAR (not the VIOS). Root recommended for full errpt.
#
# Usage:
#   vios_res_client.sh discover              # auto-discover devices -> inventory
#   vios_res_client.sh adapters              # lscfg location codes + virtual slot map (fcs*/ent*)
#   vios_res_client.sh snapshot <label>      # one-shot capture (e.g. before/after)
#   vios_res_client.sh monitor <seconds>     # continuous ping+disk I/O monitor
#   vios_res_client.sh test <label> shutdown <secs>   # snapshot(before)+monitor+snapshot(after_shutdown)
#   vios_res_client.sh test <label> restart [timeout] # snapshot(before)+monitor+wait for VIOS up+snapshot(after_restart)
#   vios_res_client.sh -h                    # help

#-----------------------------#
# Global settings             #
#-----------------------------#
v_version="1.3 20260807"
v_run_ts=$(date '+%Y%m%d_%H%M%S')
v_hostname=$(hostname)
# Results folder now includes the hostname so multiple LPARs don't clash.
v_basedir="/tmp/vios_res_${v_hostname}_${v_run_ts}"

# Timeout settings (seconds)
t_timeout_cmd=15

# Default timeout for restart wait (seconds) - how long to wait for VIOS to come back
t_restart_timeout=600

# Ping interval during restart wait (seconds)
t_ping_interval=5

# Runtime discovered values (filled by f_discover)
v_ping_target=""
v_net_ifaces=""
v_hdisks=""
v_fcs_adapters=""
v_ent_adapters=""

# Network targets file (per-interface ping targets) - same directory as this script
v_script_dir=$(cd "$(dirname "$0")" && pwd)
v_net_targets_file="${v_script_dir}/vios_res_net_targets.conf"

#-----------------------------#
# Logging helpers (Layer 2)   #
#-----------------------------#
v_logfile="${v_basedir}/vios_res_client.log"

# f_log LEVEL MESSAGE  -> append to master log + echo to console
function f_log {
    v_level="$1"
    v_message="$2"
    v_ts=$(date '+%Y-%m-%d %H:%M:%S')
    echo "[${v_ts}] [${v_level}] ${v_message}" >> "${v_logfile}"
    if [[ "${v_level}" == "ERROR" ]]; then
        echo "ERROR: ${v_message}" >&2
    else
        echo "[${v_level}] ${v_message}"
    fi
}

# f_ensure_dirs -> create base result directories
function f_ensure_dirs {
    mkdir -p "${v_basedir}" 2>/dev/null
}

# f_prompt_yes PROMPT
# Ask the user a yes/no question and only return 0 when they type "yes".
# Used because the VIOS may NOT be pingable, so we cannot auto-detect "down".
# The operator confirms manually (e.g. after shutting the VIOS from the HMC).
function f_prompt_yes {
    typeset v_prompt="$1"
    typeset v_ans=""
    while true; do
        printf "%s (type 'yes' to confirm, 'no' to abort): " "${v_prompt}"
        read v_ans
        case "${v_ans}" in
            yes|YES|Yes) return 0 ;;
            no|NO|No)    return 1 ;;
            *) echo "    Please type 'yes' or 'no'." ;;
        esac
    done
}

# f_force_path_detect [OUTFILE]
# During a VIOS shutdown the client FC/vSCSI paths may still show Enabled until
# AIX actually tries I/O over them. This forces AIX to re-evaluate the device
# paths (cfgmgr) and, if any hdisks were given, drives a little I/O so the dead
# path is marked Failed. Then it captures lspath so we can see the Failed path.
function f_force_path_detect {
    typeset v_out="$1"
    [[ -z "${v_out}" ]] && v_out="${v_basedir}/force_path_detect_${v_run_ts}.txt"
    f_ensure_dirs
    [[ -z "${v_hdisks}" ]] && v_hdisks=$(f_detect_hdisks)

    f_log "INFO" "Forcing path re-detection (cfgmgr + I/O) so the failed FC path shows up..."

    # Drive a small read on each disk so AIX exercises (and fails) the dead path.
    for v_d in ${v_hdisks}; do
        f_run "dd if=/dev/${v_d} of=/dev/null bs=64k count=16 2>&1" "${v_out}" "I/O probe on ${v_d} (trigger path failure detection)"
    done

    # cfgmgr makes AIX rescan the device tree; a dead virtual/FC path is then
    # detected and its state moves to Failed/Missing.
    f_run "cfgmgr" "${v_out}" "cfgmgr - rescan device tree to detect failed path"

    # Give AIX a moment to update path state, then capture lspath result.
    sleep 5
    f_run "lspath" "${v_out}" "MPIO path status after cfgmgr (look for Failed paths)"
    for v_d in ${v_hdisks}; do
        f_run "lspath -l ${v_d} -H -F 'name path_id parent connection status'" "${v_out}" "Paths for ${v_d} after cfgmgr"
    done

    v_failed=$(lspath 2>/dev/null | grep -i "Failed")
    if [[ -n "${v_failed}" ]]; then
        f_log "INFO" "Failed path(s) confirmed after cfgmgr."
        echo ">>> Failed path(s) detected (see ${v_out}). <<<"
    else
        f_log "WARN" "No Failed paths seen even after cfgmgr - check manually with lspath."
        echo ">>> WARNING: No Failed paths detected yet. Check 'lspath' manually. <<<"
    fi
    echo "Path-detect log: ${v_out}"
}

# f_verify_recovery [OUTFILE]
# When the VIOS comes back, confirm the paths recovered. Checks:
#   1) errpt for path-recovery entries (PATH_HAS_RECOVERED / DISK path events)
#   2) lspath shows the paths Enabled again (no Failed)
# If paths are still Failed, tries chpath -s enable and re-checks lspath.
function f_verify_recovery {
    typeset v_out="$1"
    [[ -z "${v_out}" ]] && v_out="${v_basedir}/verify_recovery_${v_run_ts}.txt"
    f_ensure_dirs
    [[ -z "${v_hdisks}" ]] && v_hdisks=$(f_detect_hdisks)

    f_log "INFO" "Verifying path recovery (errpt + lspath)..."

    # 1) errpt evidence of recovery. PATH_HAS_RECOVERED / DISK_ERR path events
    #    show AIX brought the path back after the VIOS returned.
    f_run "errpt | head -40" "${v_out}" "errpt (recent) - look for PATH_HAS_RECOVERED / path recovery"
    f_run "errpt -a | grep -iE 'PATH_HAS_RECOVERED|path.*recover|PATH_HAS_FAILED' | head -40" "${v_out}" "errpt recovery-related entries"

    # 2) lspath current state
    f_run "lspath" "${v_out}" "MPIO path status (should be Enabled again)"

    v_still_failed=$(lspath 2>/dev/null | grep -i "Failed")
    if [[ -z "${v_still_failed}" ]]; then
        f_log "INFO" "All paths Enabled again - recovery confirmed by lspath."
        echo ">>> All paths Enabled again. Recovery confirmed. <<<"
    else
        f_log "WARN" "Paths still Failed - attempting chpath -s enable..."
        echo ">>> Paths still Failed. Running chpath to re-enable... <<<"
        v_failed_paths=$(lspath -H -F "name path_id parent connection status" 2>/dev/null \
            | awk 'NR>1 && $NF ~ /Failed|Missing/ {print $1" "$3" "$4}')
        echo "${v_failed_paths}" | while read v_fdisk v_fparent v_fconn; do
            [[ -z "${v_fdisk}" ]] && continue
            f_run "chpath -l ${v_fdisk} -p ${v_fparent} -w '${v_fconn}' -s enable" "${v_out}" "chpath enable ${v_fdisk} via ${v_fparent}"
        done
        sleep 5
        f_run "lspath" "${v_out}" "MPIO path status after chpath enable"
        v_still_failed2=$(lspath 2>/dev/null | grep -i "Failed")
        if [[ -z "${v_still_failed2}" ]]; then
            f_log "INFO" "Paths recovered after chpath (chpath shows Enabled)."
            echo ">>> Paths recovered after chpath. <<<"
        else
            f_log "WARN" "Some paths still Failed even after chpath."
            echo ">>> WARNING: Some paths still Failed after chpath. Check ${v_out}. <<<"
        fi
    fi
    echo "Recovery-verify log: ${v_out}"
}


# f_run  COMMAND  OUTFILE  DESCRIPTION
# Runs a command with a timeout, writing a header + full output to OUTFILE.
# This is the "evidence capture" that replaces screenshots.
function f_run {
    v_command="$1"
    v_outfile="$2"
    v_desc="$3"
    v_ts=$(date '+%Y-%m-%d %H:%M:%S')

    {
        echo "=================================================================="
        echo "Timestamp : ${v_ts}"
        echo "Host      : ${v_hostname}"
        echo "Purpose   : ${v_desc}"
        echo "Command   : ${v_command}"
        echo "------------------------------------------------------------------"
    } >> "${v_outfile}"

    if command -v timeout >/dev/null 2>&1; then
        timeout ${t_timeout_cmd} sh -c "${v_command}" >> "${v_outfile}" 2>&1
        v_rc=$?
    else
        sh -c "${v_command}" >> "${v_outfile}" 2>&1
        v_rc=$?
    fi

    if [[ ${v_rc} -eq 124 || ${v_rc} -eq 143 ]]; then
        echo "[WARN] command timed out after ${t_timeout_cmd}s" >> "${v_outfile}"
        f_log "WARN" "Timed out: ${v_desc}"
    elif [[ ${v_rc} -ne 0 ]]; then
        f_log "WARN" "Non-zero exit (${v_rc}): ${v_desc}"
    fi
    echo "" >> "${v_outfile}"
    return ${v_rc}
}

#-----------------------------#
# Layer 1 - Auto discovery    #
#-----------------------------#

# f_detect_net_ifaces -> list active en* interfaces (those with an IP)
function f_detect_net_ifaces {
    # netstat -in lists interfaces; keep en* that are not link/loopback
    netstat -in 2>/dev/null \
        | awk '$1 ~ /^en[0-9]+/ {print $1}' \
        | sort -u
}

# f_detect_ping_target -> default gateway (best auto ping target)
function f_detect_ping_target {
    netstat -rn 2>/dev/null \
        | awk '$1 == "default" {print $2; exit}'
}

# f_detect_hdisks -> all hdisks known to MPIO (have >1 path ideally)
function f_detect_hdisks {
    lsdev -Cc disk 2>/dev/null \
        | awk '$1 ~ /^hdisk[0-9]+/ && $2 == "Available" {print $1}' \
        | sort -u
}

# f_detect_fcs_adapters -> all fcsX (FC / virtual FC) adapters
# On a VIOS client using NPIV these are virtual FC adapters mapped to a VIOS vfchost.
function f_detect_fcs_adapters {
    lsdev -Cc adapter 2>/dev/null \
        | awk '$1 ~ /^fcs[0-9]+/ {print $1}' \
        | sort -u
}

# f_detect_ent_adapters -> all entX adapters (virtual ethernet / SEA client side)
function f_detect_ent_adapters {
    lsdev -Cc adapter 2>/dev/null \
        | awk '$1 ~ /^ent[0-9]+/ {print $1}' \
        | sort -u
}

# f_get_loc DEVICE -> physical location code of a device (2nd field of lsdev -Cc)
# Falls back to lscfg -l output if lsdev returns nothing.
function f_get_loc {
    typeset v_dev="$1"
    typeset v_loc=""
    # lsdev -Cl <dev> -F physloc gives just the location code (AIX)
    v_loc=$(lsdev -Cl "${v_dev}" -F physloc 2>/dev/null | head -1)
    if [[ -z "${v_loc}" ]]; then
        # lscfg -l fcs0  ->  fcs0   U8284.22A.21ABCDE-V3-C12-T1  Virtual Fibre Channel Client Adapter
        v_loc=$(lscfg -l "${v_dev}" 2>/dev/null | awk -v d="${v_dev}" '$1 == d {print $2; exit}')
    fi
    echo "${v_loc}"
}

# f_parse_slot LOCATION_CODE
# Location code examples:
#   Virtual : U8284.22A.21ABCDE-V3-C12-T1   -> LPAR id V3, virtual slot C12, port T1
#   Physical: U78AA.001.WZSJ1B0-P1-C2-T2    -> planar P1, phys slot C2, port T2
# Echoes: "<type> <lpar_id> <slot> <port>"   where type = VIRTUAL | PHYSICAL | UNKNOWN
function f_parse_slot {
    typeset v_loc="$1"
    typeset v_type="UNKNOWN"
    typeset v_lparid="-"
    typeset v_slot="-"
    typeset v_port="-"

    if [[ -z "${v_loc}" ]]; then
        echo "UNKNOWN - - -"
        return
    fi

    # -V<n> present => virtual adapter, <n> is the LPAR (partition) id
    v_lparid=$(echo "${v_loc}" | sed -n 's/.*-V\([0-9][0-9]*\).*/\1/p')
    if [[ -n "${v_lparid}" ]]; then
        v_type="VIRTUAL"
    else
        v_lparid="-"
        # -P<n>- planar means a real (physical) adapter slot
        if echo "${v_loc}" | grep -q -- "-P[0-9]"; then
            v_type="PHYSICAL"
        fi
    fi

    # -C<n> => adapter slot number (virtual slot for virtual adapters)
    v_slot=$(echo "${v_loc}" | sed -n 's/.*-C\([0-9][0-9]*\).*/\1/p')
    [[ -z "${v_slot}" ]] && v_slot="-"

    # -T<n> => port number on the adapter
    v_port=$(echo "${v_loc}" | sed -n 's/.*-T\([0-9][0-9]*\).*/\1/p')
    [[ -z "${v_port}" ]] && v_port="-"

    echo "${v_type} ${v_lparid} ${v_slot} ${v_port}"
}

# f_adapter_line DEVICE
# Convenience: echo one formatted table row for DEVICE.
# Avoids "cmd | read" (which is unreliable in non-ksh shells) by using set --.
function f_adapter_line {
    typeset v_dev="$1"
    typeset v_loc
    typeset v_desc
    v_loc=$(f_get_loc "${v_dev}")
    v_desc=$(lsdev -Cl "${v_dev}" -F description 2>/dev/null | head -1)
    set -- $(f_parse_slot "${v_loc}")
    printf "%-8s %-32s %-8s %-7s %-6s %-4s %s\n" \
           "${v_dev}" "${v_loc:-(none)}" "$1" "$2" "$3" "$4" "${v_desc}"
}

# f_adapter_slot_only DEVICE -> echoes "<type> <lparid> <slot>"
function f_adapter_slot_only {
    typeset v_dev="$1"
    set -- $(f_parse_slot "$(f_get_loc "${v_dev}")")
    echo "$1 $2 $3"
}

# f_adapter_slots [OUTFILE]
# Requirement 1 + 2: use lscfg to get location codes for every fcsX and entX so we
# know WHICH virtual adapter slot of the LPAR each adapter occupies (and therefore
# which VIOS serves it). Writes a summary table plus the raw lscfg -vpl evidence.
function f_adapter_slots {
    typeset v_out="$1"
    [[ -z "${v_out}" ]] && v_out="${v_basedir}/adapter_slots_${v_run_ts}.txt"
    f_ensure_dirs

    [[ -z "${v_fcs_adapters}" ]] && v_fcs_adapters=$(f_detect_fcs_adapters)
    [[ -z "${v_ent_adapters}" ]] && v_ent_adapters=$(f_detect_ent_adapters)

    {
        echo "=================================================================="
        echo " Adapter Location Codes / Virtual Slot Map"
        echo " Host      : ${v_hostname}"
        echo " Generated : $(date)"
        echo "=================================================================="
        echo ""
        echo "Location code format:"
        echo "  Virtual  : U<model>.<serial>-V<lparid>-C<slot>-T<port>"
        echo "  Physical : U<model>.<serial>-P<planar>-C<slot>-T<port>"
        echo "  -V = LPAR (partition) id, -C = adapter slot, -T = port"
        echo ""
        printf "%-8s %-32s %-8s %-7s %-6s %-4s %s\n" \
               "DEVICE" "LOCATION CODE" "TYPE" "LPAR-ID" "SLOT" "PORT" "DESCRIPTION"
        printf "%-8s %-32s %-8s %-7s %-6s %-4s %s\n" \
               "--------" "--------------------------------" "--------" "-------" "------" "----" "-----------"

        # --- FC adapters (fcsX)  [requirement 1: lscfg location code of fcsX] ---
        for v_a in ${v_fcs_adapters}; do
            f_adapter_line "${v_a}"
        done

        # --- Ethernet adapters (entX)  [requirement 2: lscfg location code of ent] ---
        for v_a in ${v_ent_adapters}; do
            f_adapter_line "${v_a}"
        done

        echo ""
        echo "-- Virtual adapter slots in use by this LPAR --"
        for v_a in ${v_fcs_adapters} ${v_ent_adapters}; do
            set -- $(f_adapter_slot_only "${v_a}")
            if [[ "$1" == "VIRTUAL" ]]; then
                echo "  ${v_a} -> LPAR id $2, virtual slot C$3  (match slot C$3 on the HMC to find the serving VIOS)"
            fi
        done
        echo ""
        echo "-- Slot grouping (adapters sharing a slot come from the same VIOS) --"
        for v_a in ${v_fcs_adapters} ${v_ent_adapters}; do
            set -- $(f_adapter_slot_only "${v_a}")
            [[ "$1" == "VIRTUAL" ]] && echo "C$3 ${v_a}"
        done | sort -n | awk '{ if ($1 != p) { printf "\n  slot %-5s :", $1; p=$1 } printf " %s", $2 } END { print "" }'
        echo ""
        echo "NOTE: On the HMC, the client virtual slot C<n> listed above is mapped to a"
        echo "      server slot on one of the VIOS (vfchost for fcs, SEA/vent for ent)."
        echo "      Two adapters in DIFFERENT slots normally mean two different VIOS =>"
        echo "      that is what gives the LPAR its dual-VIOS redundancy."
    } >> "${v_out}"

    # --- Raw lscfg evidence for each adapter (requirement: 'it should have lscfg') ---
    for v_a in ${v_fcs_adapters}; do
        f_run "lscfg -vpl ${v_a}" "${v_out}" "lscfg location code + VPD for FC adapter ${v_a}"
        # WWPN is needed to correlate with the VIOS vfchost / SAN zoning
        f_run "lscfg -vl ${v_a} | grep -i 'Network Address'" "${v_out}" "WWPN of ${v_a}"
    done
    for v_a in ${v_ent_adapters}; do
        f_run "lscfg -vpl ${v_a}" "${v_out}" "lscfg location code + VPD for ethernet adapter ${v_a}"
    done

    # Parent/child relationship shows which vscsi/fscsi hangs off which adapter
    f_run "lsdev -Cc adapter" "${v_out}" "All adapters (lsdev -Cc adapter)"
    f_run "lsdev -Cc adapter -F 'name status physloc description'" "${v_out}" "Adapter location codes (lsdev physloc)"

    f_log "INFO" "Adapter slot map written: ${v_out}"
    echo "Adapter slot map: ${v_out}"
}

# f_discover -> populate globals + write a human-readable inventory file
function f_discover {
    f_ensure_dirs
    v_inv="${v_basedir}/inventory_${v_run_ts}.txt"

    f_log "INFO" "Auto-discovering client devices..."

    v_net_ifaces=$(f_detect_net_ifaces)
    v_hdisks=$(f_detect_hdisks)
    v_fcs_adapters=$(f_detect_fcs_adapters)
    v_ent_adapters=$(f_detect_ent_adapters)
    if [[ -z "${v_ping_target}" ]]; then
        v_ping_target=$(f_detect_ping_target)
    fi

    {
        echo "=================================================================="
        echo " VIOS Resilience - Client Inventory"
        echo " Host      : ${v_hostname}"
        echo " Generated : $(date)"
        echo "=================================================================="
        echo ""
        echo "-- Network interfaces (en*) --"
        if [[ -n "${v_net_ifaces}" ]]; then
            echo "${v_net_ifaces}"
        else
            echo "(none found)"
        fi
        echo ""
        echo "-- Ping target (default gateway) --"
        echo "${v_ping_target:-(not detected - specify with -t)}"
        echo ""
        echo "-- Network targets file --"
        if [[ -f "${v_net_targets_file}" ]]; then
            echo "Found: ${v_net_targets_file}"
            cat "${v_net_targets_file}"
        else
            echo "(not configured - see 'netsetup' command to create)"
        fi
        echo ""
        echo "-- Disks (hdisk*) --"
        if [[ -n "${v_hdisks}" ]]; then
            echo "${v_hdisks}"
        else
            echo "(none found)"
        fi
        echo ""
        echo "-- FC adapters (fcs*) with lscfg location codes --"
        if [[ -n "${v_fcs_adapters}" ]]; then
            printf "  %-8s %-32s %-8s %-7s %-6s %-4s %s\n" \
                   "DEVICE" "LOCATION CODE" "TYPE" "LPAR-ID" "SLOT" "PORT" "DESCRIPTION"
            for v_a in ${v_fcs_adapters}; do
                echo "  $(f_adapter_line "${v_a}")"
            done
        else
            echo "(none found)"
        fi
        echo ""
        echo "-- Ethernet adapters (ent*) with lscfg location codes --"
        if [[ -n "${v_ent_adapters}" ]]; then
            printf "  %-8s %-32s %-8s %-7s %-6s %-4s %s\n" \
                   "DEVICE" "LOCATION CODE" "TYPE" "LPAR-ID" "SLOT" "PORT" "DESCRIPTION"
            for v_a in ${v_ent_adapters}; do
                echo "  $(f_adapter_line "${v_a}")"
            done
        else
            echo "(none found)"
        fi
        echo ""
        echo "-- Virtual adapter slots of this LPAR (from -C<n> in location code) --"
        for v_a in ${v_fcs_adapters} ${v_ent_adapters}; do
            set -- $(f_adapter_slot_only "${v_a}")
            [[ "$1" == "VIRTUAL" ]] && echo "  ${v_a} : LPAR id $2, virtual slot C$3"
        done
        echo ""
        echo "-- MPIO paths (lspath) --"
        lspath 2>/dev/null
    } > "${v_inv}"

    # Detailed lscfg -vpl evidence for every fcs/ent adapter in its own file
    f_adapter_slots

    f_log "INFO" "Inventory written: ${v_inv}"
    echo ""
    cat "${v_inv}"
}

#-----------------------------#
# Network targets management  #
#-----------------------------#

# f_load_net_targets
# Load per-interface ping targets from config file.
# File format (one line per interface):
#   en0=10.1.1.1
#   en1=10.2.2.1
#   en2=192.168.1.1
# Returns: populates associative-like variables v_net_target_<iface>=<ip>
#          and v_net_target_ifaces (space-separated list of ALL interface keys
#          found in the config file, regardless of whether they are active on
#          this LPAR). This ensures every user-supplied ping target is tested.
function f_load_net_targets {
    if [[ ! -f "${v_net_targets_file}" ]]; then
        return 1
    fi
    v_net_target_ifaces=""
    # Read file and set variables
    while IFS='=' read v_iface v_ip; do
        # Skip comments and empty lines
        [[ "${v_iface}" == \#* ]] && continue
        [[ -z "${v_iface}" ]] && continue
        v_iface=$(echo "${v_iface}" | tr -d ' ')
        v_ip=$(echo "${v_ip}" | tr -d ' ')
        [[ -z "${v_ip}" ]] && continue
        eval "v_net_target_${v_iface}=\"${v_ip}\""
        v_net_target_ifaces="${v_net_target_ifaces} ${v_iface}"
    done < "${v_net_targets_file}"
    return 0
}

# f_get_net_target IFACE
# Get the ping target for a specific interface. Returns the IP or empty.
function f_get_net_target {
    v_iface="$1"
    eval "echo \"\${v_net_target_${v_iface}}\""
}

# f_setup_net_targets
# Interactive setup: for each discovered interface, ask user for a ping target IP.
# Writes to v_net_targets_file for future use.
function f_setup_net_targets {
    [[ -z "${v_net_ifaces}" ]] && v_net_ifaces=$(f_detect_net_ifaces)

    if [[ -z "${v_net_ifaces}" ]]; then
        f_log "ERROR" "No network interfaces discovered. Cannot setup targets."
        return 1
    fi

    echo ""
    echo "=================================================================="
    echo " Network Targets Setup"
    echo " Configure a ping target IP for each network interface."
    echo " This allows testing connectivity on ALL networks during failover."
    echo "=================================================================="
    echo ""

    if [[ -f "${v_net_targets_file}" ]]; then
        echo "Existing config found: ${v_net_targets_file}"
        cat "${v_net_targets_file}"
        echo ""
        printf "Overwrite? (y/n): "
        read v_answer
        if [[ "${v_answer}" != "y" && "${v_answer}" != "Y" ]]; then
            echo "Keeping existing config."
            return 0
        fi
    fi

    # Show interface IPs to help user decide targets
    echo ""
    echo "Current interface configuration:"
    echo "---"
    for v_if in ${v_net_ifaces}; do
        v_ifip=$(ifconfig ${v_if} 2>/dev/null | awk '/inet / {print $2}')
        echo "  ${v_if} : ${v_ifip:-(no IP)}"
    done
    echo "---"
    echo ""
    echo "For each interface, enter a target IP to ping (e.g. gateway or remote host"
    echo "reachable via that interface). Press Enter to skip an interface."
    echo ""

    # Create/overwrite config
    {
        echo "# vios_res_client network targets configuration"
        echo "# Format: interface=target_ip"
        echo "# Generated: $(date)"
        echo "#"
    } > "${v_net_targets_file}"

    for v_if in ${v_net_ifaces}; do
        v_ifip=$(ifconfig ${v_if} 2>/dev/null | awk '/inet / {print $2}')
        printf "  Target IP for ${v_if} (local IP: ${v_ifip:-(none)}): "
        read v_target_ip
        if [[ -n "${v_target_ip}" ]]; then
            echo "${v_if}=${v_target_ip}" >> "${v_net_targets_file}"
            echo "    -> ${v_if}=${v_target_ip} saved."
        else
            echo "    -> ${v_if} skipped."
        fi
    done

    echo ""
    echo "Config saved: ${v_net_targets_file}"
    echo "This file will be used automatically for multi-network ping in future tests."
    cat "${v_net_targets_file}"
}

#-----------------------------#
# Layer 2 - Snapshot capture  #
#-----------------------------#

# f_snapshot LABEL
# One-shot capture of network + disk state. Call with "before" then "after".
function f_snapshot {
    typeset v_label="$1"
    [[ -z "${v_label}" ]] && v_label="snapshot"
    f_ensure_dirs

    # make sure discovery has run so device lists are populated
    [[ -z "${v_net_ifaces}" ]] && v_net_ifaces=$(f_detect_net_ifaces)
    [[ -z "${v_hdisks}" ]] && v_hdisks=$(f_detect_hdisks)
    [[ -z "${v_ping_target}" ]] && v_ping_target=$(f_detect_ping_target)
    [[ -z "${v_fcs_adapters}" ]] && v_fcs_adapters=$(f_detect_fcs_adapters)
    [[ -z "${v_ent_adapters}" ]] && v_ent_adapters=$(f_detect_ent_adapters)

    v_snap_ts=$(date '+%Y%m%d_%H%M%S')
    v_snap="${v_basedir}/snapshot_${v_label}_${v_snap_ts}.txt"
    : > "${v_snap}"
    f_log "INFO" "Capturing snapshot '${v_label}' -> ${v_snap}"

    # --- Adapter location codes / virtual slots (fcsX + entX via lscfg) ---
    # Captured in every snapshot so a before/after diff shows if an adapter or its
    # slot disappeared/changed when a VIOS went down.
    {
        echo "=================================================================="
        echo "Timestamp : $(date '+%Y-%m-%d %H:%M:%S')"
        echo "Host      : ${v_hostname}"
        echo "Purpose   : Adapter location codes + virtual slot map (fcs* / ent*)"
        echo "Command   : lscfg -l <dev> / lsdev -Cl <dev> -F physloc"
        echo "------------------------------------------------------------------"
        printf "%-8s %-32s %-8s %-7s %-6s %-4s %s\n" \
               "DEVICE" "LOCATION CODE" "TYPE" "LPAR-ID" "SLOT" "PORT" "DESCRIPTION"
        for v_a in ${v_fcs_adapters} ${v_ent_adapters}; do
            f_adapter_line "${v_a}"
        done
        echo ""
    } >> "${v_snap}"

    # Per-adapter lscfg detail: fcsX location code (requirement 1)
    for v_a in ${v_fcs_adapters}; do
        f_run "lscfg -vl ${v_a}" "${v_snap}" "lscfg location code/VPD for FC adapter ${v_a}"
    done
    # Per-adapter lscfg detail: entX location code (requirement 2)
    for v_a in ${v_ent_adapters}; do
        f_run "lscfg -vl ${v_a}" "${v_snap}" "lscfg location code/VPD for ethernet adapter ${v_a}"
    done

    # --- Disk / MPIO ---
    f_run "lspath" "${v_snap}" "MPIO path status (all disks)"
    for v_d in ${v_hdisks}; do
        f_run "lspath -l ${v_d} -H -F 'name path_id parent connection status'" "${v_snap}" "Paths for ${v_d}"
        # hcheck_interval decides if Failed paths auto-recover when the VIOS returns.
        # If 0 => paths stay Failed and you must run 'chpath -s enable' (or this script's 'recover').
        f_run "lsattr -El ${v_d} -a hcheck_interval -a hcheck_mode -a reserve_policy -a algorithm 2>/dev/null" "${v_snap}" "MPIO attrs for ${v_d} (hcheck_interval => auto-recovery)"
    done

    # lsmpio gives per-path detail on newer AIX; ignore if not present
    f_run "lsmpio -q 2>/dev/null; lsmpio 2>/dev/null" "${v_snap}" "lsmpio detail (if available)"

    # fscsi child of each fcs adapter -> tells which fcs serves which disk path
    for v_a in ${v_fcs_adapters}; do
        f_run "lsdev -Cc driver -p ${v_a}; lsdev -C -p ${v_a}" "${v_snap}" "Children of ${v_a} (fscsi devices)"
    done

    # --- Network ---
    f_run "netstat -in" "${v_snap}" "Network interface list"
    for v_if in ${v_net_ifaces}; do
        f_run "entstat -d ${v_if} 2>/dev/null | head -40" "${v_snap}" "entstat ${v_if} (link/state)"
    done
    f_run "netstat -rn" "${v_snap}" "Routing table"

    # --- Multi-network ping test (if targets configured) ---
    # Iterate over ALL targets from the config file (v_net_target_ifaces),
    # not just the discovered interfaces (v_net_ifaces), so that every
    # user-supplied IP is tested even if the LPAR has fewer active en* devices.
    if [[ -f "${v_net_targets_file}" ]]; then
        f_load_net_targets
        for v_if in ${v_net_target_ifaces}; do
            v_tgt=$(f_get_net_target "${v_if}")
            if [[ -n "${v_tgt}" ]]; then
                f_run "ping -c 3 -w 5 ${v_tgt} 2>&1" "${v_snap}" "Ping test ${v_if} -> ${v_tgt}"
            fi
        done
    fi

    # --- Errors ---
    f_run "errpt | head -30" "${v_snap}" "Recent error report (errpt)"

    f_log "INFO" "Snapshot '${v_label}' complete."
    echo "Saved: ${v_snap}"
}

#-----------------------------#
# Layer 2 - Continuous monitor#
#-----------------------------#

# f_monitor SECONDS
# Runs a continuous ping and a continuous disk read loop for SECONDS,
# logging timestamped results. Detects packet loss and I/O stalls during failover.
function f_monitor {
    v_secs="$1"
    v_msg="$2"
    [[ -z "${v_secs}" ]] && v_secs=120
    [[ -z "${v_msg}" ]] && v_msg="Now trigger the VIOS shutdown/restart from the HMC while this runs..."
    f_ensure_dirs

    [[ -z "${v_ping_target}" ]] && v_ping_target=$(f_detect_ping_target)
    [[ -z "${v_hdisks}" ]] && v_hdisks=$(f_detect_hdisks)
    [[ -z "${v_net_ifaces}" ]] && v_net_ifaces=$(f_detect_net_ifaces)

    if [[ -z "${v_ping_target}" ]]; then
        f_log "ERROR" "No ping target. Re-run with -t <ip>."
        return 1
    fi

    v_ping_log="${v_basedir}/monitor_ping_${v_run_ts}.txt"
    v_io_log="${v_basedir}/monitor_disk_${v_run_ts}.txt"
    v_multinet_log="${v_basedir}/monitor_multinet_${v_run_ts}.txt"

    f_log "INFO" "Monitoring for ${v_secs}s. Ping target: ${v_ping_target}"
    echo "${v_msg}"


    # --- background continuous ping (1/sec, timestamped) ---
    (
        v_end=$(( $(date +%s) + v_secs ))
        while [[ $(date +%s) -lt ${v_end} ]]; do
            v_line=$(ping -c 1 -w 2 "${v_ping_target}" 2>/dev/null | grep -i "time=")
            if [[ -n "${v_line}" ]]; then
                echo "$(date '+%H:%M:%S') OK   ${v_line}"
            else
                echo "$(date '+%H:%M:%S') LOSS no reply from ${v_ping_target}"
            fi
            sleep 1
        done
    ) >> "${v_ping_log}" 2>&1 &
    v_ping_pid=$!

    # --- background multi-network ping (if targets configured) ---
    # Use v_net_target_ifaces (all config entries) instead of v_net_ifaces
    # (discovered active interfaces) so every user-supplied IP is tested.
    v_multinet_pid=""
    if [[ -f "${v_net_targets_file}" ]]; then
        f_load_net_targets
        v_cfg_ifaces="${v_net_target_ifaces}"
        (
            v_end=$(( $(date +%s) + v_secs ))
            while [[ $(date +%s) -lt ${v_end} ]]; do
                for v_if in ${v_cfg_ifaces}; do
                    v_tgt=$(f_get_net_target "${v_if}")
                    if [[ -n "${v_tgt}" ]]; then
                        v_reply=$(ping -c 1 -w 2 "${v_tgt}" 2>/dev/null | grep -i "time=")
                        if [[ -n "${v_reply}" ]]; then
                            echo "$(date '+%H:%M:%S') ${v_if} OK   ${v_tgt} ${v_reply}"
                        else
                            echo "$(date '+%H:%M:%S') ${v_if} LOSS ${v_tgt} no reply"
                        fi
                    fi
                done
                sleep 2
            done
        ) >> "${v_multinet_log}" 2>&1 &
        v_multinet_pid=$!
        f_log "INFO" "Multi-network monitor started (targets from ${v_net_targets_file})"
    fi

    # --- background disk read loop (detects I/O stall on first hdisk) ---
    v_test_disk=$(echo "${v_hdisks}" | head -1)
    (
        v_end=$(( $(date +%s) + v_secs ))
        while [[ $(date +%s) -lt ${v_end} ]]; do
            v_t0=$(date +%s)
            dd if=/dev/${v_test_disk} of=/dev/null bs=64k count=16 2>/dev/null
            v_rc=$?
            v_t1=$(date +%s)
            v_dur=$(( v_t1 - v_t0 ))
            if [[ ${v_rc} -ne 0 ]]; then
                echo "$(date '+%H:%M:%S') ERROR dd read from ${v_test_disk} failed rc=${v_rc}"
            elif [[ ${v_dur} -ge 3 ]]; then
                echo "$(date '+%H:%M:%S') SLOW  dd read took ${v_dur}s (possible failover stall)"
            else
                echo "$(date '+%H:%M:%S') OK    dd read from ${v_test_disk} (${v_dur}s)"
            fi
            sleep 1
        done
    ) >> "${v_io_log}" 2>&1 &
    v_io_pid=$!

    # wait for all to finish
    wait ${v_ping_pid} 2>/dev/null
    wait ${v_io_pid} 2>/dev/null
    [[ -n "${v_multinet_pid}" ]] && wait ${v_multinet_pid} 2>/dev/null

    # summarize
    v_loss=$(grep -c "LOSS" "${v_ping_log}" 2>/dev/null)
    v_ioerr=$(grep -cE "ERROR|SLOW" "${v_io_log}" 2>/dev/null)
    f_log "INFO" "Monitor done. Ping losses: ${v_loss}; Disk stalls/errors: ${v_ioerr}"
    echo "Ping log : ${v_ping_log}  (LOSS events: ${v_loss})"
    echo "Disk log : ${v_io_log}  (stall/error events: ${v_ioerr})"

    if [[ -f "${v_net_targets_file}" ]]; then
        v_multinet_loss=$(grep -c "LOSS" "${v_multinet_log}" 2>/dev/null)
        echo "Multi-net: ${v_multinet_log}  (LOSS events: ${v_multinet_loss})"
    fi
}

#-----------------------------#
# VIOS restart detection      #
#-----------------------------#

# f_monitor_restart SECONDS VIOS_IP LABEL
# Restart-aware monitor: runs continuous monitoring in the background while the
# operator shuts down / restarts the VIOS from the HMC.
#
# IMPORTANT: The VIOS itself may NOT be pingable (no service IP, isolated mgmt
# network, etc.), so we do NOT rely on pinging the VIOS to decide "up"/"down".
# Instead the operator confirms interactively ("is the VIOS down already?").
# Once confirmed down, we run cfgmgr + I/O to force AIX to mark the FC/vSCSI
# path Failed and capture lspath. On recovery we check errpt + lspath/chpath.
function f_monitor_restart {
    typeset v_mon_secs="$1"
    typeset v_mon_vios_ip="$2"
    typeset v_mon_label="$3"
    [[ -z "${v_mon_secs}" ]] && v_mon_secs=180


    f_ensure_dirs
    [[ -z "${v_ping_target}" ]] && v_ping_target=$(f_detect_ping_target)
    [[ -z "${v_hdisks}" ]] && v_hdisks=$(f_detect_hdisks)
    [[ -z "${v_net_ifaces}" ]] && v_net_ifaces=$(f_detect_net_ifaces)

    v_ping_log="${v_basedir}/monitor_ping_${v_run_ts}.txt"
    v_io_log="${v_basedir}/monitor_disk_${v_run_ts}.txt"
    v_multinet_log="${v_basedir}/monitor_multinet_${v_run_ts}.txt"

    f_log "INFO" "Restart monitor: ${v_mon_secs}s window (VIOS confirmed by operator, not ping)."
    v_vios_ping_log="${v_basedir}/monitor_vios_events_${v_run_ts}.txt"



    # --- background continuous ping to default gateway (1/sec) ---
    if [[ -n "${v_ping_target}" ]]; then
        (
            v_end=$(( $(date +%s) + v_mon_secs + ${t_restart_timeout} ))
            while [[ $(date +%s) -lt ${v_end} ]]; do
                v_line=$(ping -c 1 -w 2 "${v_ping_target}" 2>/dev/null | grep -i "time=")
                if [[ -n "${v_line}" ]]; then
                    echo "$(date '+%H:%M:%S') OK   ${v_line}"
                else
                    echo "$(date '+%H:%M:%S') LOSS no reply from ${v_ping_target}"
                fi
                sleep 1
            done
        ) >> "${v_ping_log}" 2>&1 &
        v_bg_ping_pid=$!
    fi

    # --- background multi-network ping (if targets configured) ---
    # Use v_net_target_ifaces (all config entries) instead of v_net_ifaces
    # (discovered active interfaces) so every user-supplied IP is tested.
    v_bg_multinet_pid=""
    if [[ -f "${v_net_targets_file}" ]]; then
        f_load_net_targets
        v_cfg_ifaces="${v_net_target_ifaces}"
        (
            v_end=$(( $(date +%s) + v_mon_secs + ${t_restart_timeout} ))
            while [[ $(date +%s) -lt ${v_end} ]]; do
                for v_if in ${v_cfg_ifaces}; do
                    v_tgt=$(f_get_net_target "${v_if}")
                    if [[ -n "${v_tgt}" ]]; then
                        v_reply=$(ping -c 1 -w 2 "${v_tgt}" 2>/dev/null | grep -i "time=")
                        if [[ -n "${v_reply}" ]]; then
                            echo "$(date '+%H:%M:%S') ${v_if} OK   ${v_tgt} ${v_reply}"
                        else
                            echo "$(date '+%H:%M:%S') ${v_if} LOSS ${v_tgt} no reply"
                        fi
                    fi
                done
                sleep 2
            done
        ) >> "${v_multinet_log}" 2>&1 &
        v_bg_multinet_pid=$!
    fi

    # --- background disk read loop ---
    v_test_disk=$(echo "${v_hdisks}" | head -1)
    (
        v_end=$(( $(date +%s) + v_mon_secs + ${t_restart_timeout} ))
        while [[ $(date +%s) -lt ${v_end} ]]; do
            v_t0=$(date +%s)
            dd if=/dev/${v_test_disk} of=/dev/null bs=64k count=16 2>/dev/null
            v_rc=$?
            v_t1=$(date +%s)
            v_dur=$(( v_t1 - v_t0 ))
            if [[ ${v_rc} -ne 0 ]]; then
                echo "$(date '+%H:%M:%S') ERROR dd read from ${v_test_disk} failed rc=${v_rc}"
            elif [[ ${v_dur} -ge 3 ]]; then
                echo "$(date '+%H:%M:%S') SLOW  dd read took ${v_dur}s (possible failover stall)"
            else
                echo "$(date '+%H:%M:%S') OK    dd read from ${v_test_disk} (${v_dur}s)"
            fi
            sleep 1
        done
    ) >> "${v_io_log}" 2>&1 &
    v_bg_io_pid=$!

    # --- Foreground: operator confirms down -> force path detect -> during snapshot
    #     -> operator confirms up -> verify recovery -> after snapshot ---
    echo "Monitoring started (gateway ping + disk I/O run in the background)."
    echo "RESTART / SHUT DOWN THE TARGET VIOS NOW from the HMC."
    echo "The VIOS may not be pingable, so this script will ASK you to confirm state."
    echo ""


    # Phase 1: Wait for the OPERATOR to confirm the VIOS is down.
    # The VIOS may not be pingable, so we do not auto-detect via ping.
    v_down_detected=0
    echo ""
    if f_prompt_yes "Have you shut down the VIOS and is it DOWN already?"; then
        v_down_detected=1
        f_log "INFO" "Operator confirmed VIOS is DOWN."
    else
        f_log "WARN" "Operator did not confirm VIOS down; capturing current state."
    fi

    # Phase 2: Force AIX to notice the failed FC/vSCSI path, then capture "during".
    # cfgmgr rescans + a small I/O probe makes the dead path transition to Failed.
    echo ""
    echo "Now forcing path re-detection (cfgmgr + I/O) so lspath shows the Failed path..."
    f_force_path_detect "${v_basedir}/during_restart_pathdetect_${v_run_ts}.txt"
    f_snapshot "${v_mon_label}_during_restart"

    # Phase 3: Wait for the OPERATOR to confirm the VIOS is back up.
    echo ""
    if f_prompt_yes "Has the VIOS been restarted and is it UP again?"; then
        v_came_up=1
        f_log "INFO" "Operator confirmed VIOS is UP again."
    else
        v_came_up=0
        f_log "WARN" "Operator did not confirm VIOS up."
    fi

    # Phase 4: Verify path recovery (errpt + lspath), chpath if needed, then snapshot.
    if [[ ${v_came_up} -eq 1 ]]; then
        echo "VIOS reported UP. Verifying MPIO path recovery..."
        f_log "INFO" "Verifying path recovery via errpt + lspath (+chpath if needed)..."
        f_verify_recovery "${v_basedir}/after_restart_recovery_${v_run_ts}.txt"
        f_snapshot "${v_mon_label}_after_restart"
    else
        f_log "WARN" "VIOS not confirmed up by operator."
        echo ""
        echo ">>> WARNING: VIOS not confirmed up. Capturing current state. <<<"
        f_snapshot "${v_mon_label}_after_restart_NOTUP"
    fi


    # Kill background monitors
    kill ${v_bg_ping_pid} 2>/dev/null
    kill ${v_bg_io_pid} 2>/dev/null
    [[ -n "${v_bg_multinet_pid}" ]] && kill ${v_bg_multinet_pid} 2>/dev/null
    wait 2>/dev/null

    # Summarize
    v_loss=$(grep -c "LOSS" "${v_ping_log}" 2>/dev/null)
    v_ioerr=$(grep -cE "ERROR|SLOW" "${v_io_log}" 2>/dev/null)
    f_log "INFO" "Restart monitor done. Gateway ping losses: ${v_loss}; Disk stalls/errors: ${v_ioerr}"
    echo ""
    echo "Ping log  : ${v_ping_log}  (LOSS events: ${v_loss})"
    echo "Disk log  : ${v_io_log}  (stall/error events: ${v_ioerr})"
    echo "VIOS ping : ${v_vios_ping_log}"
    if [[ -n "${v_bg_multinet_pid}" ]]; then
        v_multinet_loss=$(grep -c "LOSS" "${v_multinet_log}" 2>/dev/null)
        echo "Multi-net : ${v_multinet_log}  (LOSS events: ${v_multinet_loss})"
    fi
}

# f_wait_vios_up VIOS_IP TIMEOUT
# Pings the VIOS IP until it responds, or times out.
# Used in "restart" mode to auto-detect when the VIOS has come back up.
# Returns 0 if VIOS came up, 1 if timed out.
function f_wait_vios_up {
    v_vios_ip="$1"
    v_wait_timeout="$2"
    [[ -z "${v_wait_timeout}" ]] && v_wait_timeout=${t_restart_timeout}

    f_log "INFO" "Waiting for VIOS (${v_vios_ip}) to come back up (timeout: ${v_wait_timeout}s)..."
    echo ""
    echo ">>> Waiting for VIOS ${v_vios_ip} to respond to ping (timeout: ${v_wait_timeout}s) <<<"
    echo "    Checking every ${t_ping_interval}s..."

    v_start=$(date +%s)
    v_end=$(( v_start + v_wait_timeout ))
    v_up=0

    while [[ $(date +%s) -lt ${v_end} ]]; do
        v_reply=$(ping -c 1 -w 3 "${v_vios_ip}" 2>/dev/null | grep -i "time=")
        if [[ -n "${v_reply}" ]]; then
            v_up=1
            v_elapsed=$(( $(date +%s) - v_start ))
            f_log "INFO" "VIOS ${v_vios_ip} is UP after ${v_elapsed}s"
            echo ""
            echo ">>> VIOS ${v_vios_ip} responded to ping after ${v_elapsed}s <<<"
            break
        fi
        printf "."
        sleep ${t_ping_interval}
    done

    if [[ ${v_up} -eq 0 ]]; then
        v_elapsed=$(( $(date +%s) - v_start ))
        f_log "WARN" "VIOS ${v_vios_ip} did NOT respond within ${v_wait_timeout}s"
        echo ""
        echo ">>> WARNING: VIOS ${v_vios_ip} did not respond within timeout (${v_wait_timeout}s) <<<"
        return 1
    fi

    # Wait an additional 30s for VIOS services to stabilize
    echo "Waiting 30s for VIOS services to stabilize..."
    sleep 30
    f_log "INFO" "Stabilization wait complete. Proceeding with after-snapshot."
    return 0
}

#-----------------------------#
# Combined test workflow      #
#-----------------------------#

# f_test LABEL MODE [SECONDS_OR_TIMEOUT]
# MODE = "shutdown" or "restart"
#   shutdown: before-snapshot -> monitor(SECONDS) -> after-snapshot (manual)
#   restart:  before-snapshot -> monitor -> wait for VIOS ping -> after-snapshot (auto)
function f_test {
    v_label="$1"
    v_mode="$2"
    v_secs="$3"
    [[ -z "${v_label}" ]] && v_label="test"
    [[ -z "${v_mode}" ]] && v_mode="shutdown"
    [[ -z "${v_secs}" ]] && v_secs=180

    # Validate mode
    if [[ "${v_mode}" != "shutdown" && "${v_mode}" != "restart" ]]; then
        f_log "ERROR" "Invalid mode '${v_mode}'. Use 'shutdown' or 'restart'."
        echo "ERROR: Invalid mode '${v_mode}'. Use 'shutdown' or 'restart'."
        echo "  shutdown: monitor for <seconds>, then capture after snapshot"
        echo "  restart:  monitor, wait for VIOS to come back up (ping), then capture after snapshot"
        return 1
    fi

    f_log "INFO" "=== TEST '${v_label}' START (mode: ${v_mode}) ==="
    f_discover

    # For restart mode we no longer PING the VIOS to detect up/down (the VIOS may
    # not be pingable). The operator confirms state interactively. We only try a
    # quick, time-bounded name resolution for the log/label - if it does not
    # resolve fast we simply carry on using the label as-is.
    if [[ "${v_mode}" == "restart" ]]; then
        if [[ -z "${v_vios_ip}" ]]; then
            # Fast hostname resolution only (no long ping wait).
            # 1) /etc/hosts lookup first (instant, no network).
            v_resolved=$(awk -v h="${v_label}" '$0 !~ /^#/ && $0 ~ h {print $1; exit}' /etc/hosts 2>/dev/null)
            # 2) getent if still empty (usually fast).
            [[ -z "${v_resolved}" ]] && v_resolved=$(getent hosts "${v_label}" 2>/dev/null | awk '{print $1; exit}')
            if [[ -n "${v_resolved}" ]]; then
                v_vios_ip="${v_resolved}"
                f_log "INFO" "Resolved VIOS '${v_label}' -> ${v_vios_ip} (state confirmed by operator, not ping)."
            else
                # Could not resolve quickly - just use the label; not required for detection.
                v_vios_ip="${v_label}"
                f_log "INFO" "Using label '${v_label}' as VIOS reference (state confirmed by operator, not ping)."
            fi
        fi
        f_log "INFO" "VIOS reference: ${v_vios_ip} (operator confirms up/down)"
    fi


    # Before snapshot: snapshot_<label>_before_<mode>_<ts>.txt
    f_snapshot "${v_label}_before_${v_mode}"

    echo ""
    if [[ "${v_mode}" == "shutdown" ]]; then
        # --- NEW FLOW ---
        # 1) Ask FIRST whether the VIOS has already been shut down from the HMC.
        # 2) Once confirmed, immediately capture the "down" evidence (force path
        #    detect + after_shutdown snapshot) - no fixed monitor wait beforehand.
        # 3) Then ask whether the VIOS has been started back up.
        # 4) Once confirmed, capture the "after restart" evidence, then package
        #    the results and tell the operator to download via the GUI.
        echo ">>> Go to the HMC now and SHUT DOWN the target VIOS. <<<"
        echo ""
        while ! f_prompt_yes "Have you shut down the VIOS and is it DOWN already?"; do
            echo "    Waiting for you to shut down the VIOS from the HMC..."
        done
        f_log "INFO" "Operator confirmed VIOS is DOWN. Capturing evidence now..."

        echo ""
        echo "Capturing status/log now (this will take a short while)..."
        # Force AIX to notice the failed FC/vSCSI path (cfgmgr + I/O) then lspath,
        # so the after snapshot clearly shows the Failed path.
        f_force_path_detect "${v_basedir}/after_shutdown_pathdetect_${v_run_ts}.txt"
        # Short monitor burst to capture ping/disk impact evidence while VIOS is down.
        f_monitor "${v_secs}" "Capturing ping + disk I/O evidence while VIOS is DOWN..."
        # After snapshot named: snapshot_<vios_name>_after_shutdown_<timestamp>.txt
        f_snapshot "${v_label}_after_shutdown"
        f_log "INFO" "Down-state capture complete."

        echo ""
        echo "Down-state capture complete."
        echo ">>> Now go to the HMC and START UP the VIOS again. <<<"
        echo ""
        while ! f_prompt_yes "Has the VIOS been started up and is it UP again?"; do
            echo "    Waiting for you to start the VIOS from the HMC..."
        done
        f_log "INFO" "Operator confirmed VIOS is UP again. Capturing recovery evidence now..."

        echo ""
        echo "Capturing recovery status/log now..."
        f_verify_recovery "${v_basedir}/after_restart_recovery_${v_run_ts}.txt"
        f_snapshot "${v_label}_after_restart"
        f_log "INFO" "Recovery capture complete."

    elif [[ "${v_mode}" == "restart" ]]; then
        echo ">>> Starting ${v_secs}s monitor. RESTART THE TARGET VIOS NOW when ready. <<<"
        echo ">>> Script will detect when VIOS goes down, capture 'during' snapshot, then wait for it to return. <<<"
        echo ""

        # Use restart-aware monitor that detects VIOS going down
        f_monitor_restart "${v_secs}" "${v_vios_ip}" "${v_label}"
    fi

    f_log "INFO" "=== TEST '${v_label}' DONE (mode: ${v_mode}). Results in ${v_basedir} ==="
    echo ""
    echo "All evidence saved under: ${v_basedir}"
    if [[ "${v_mode}" == "shutdown" ]]; then
        echo "Compare snapshot_${v_label}_after_shutdown_* vs snapshot_${v_label}_after_restart_* for path/state changes."
    else
        echo "Compare snapshot_${v_label}_before_restart_* vs snapshot_${v_label}_after_restart_* for path/state changes."
    fi
    # auto-package everything into one tar for easy transfer / reporting
    f_package
    echo ""
    echo ">>> Test complete. Download the packaged .tar.gz file above from the PowerPilot GUI (Fetch Results). <<<"
}



#-----------------------------#
# Path recovery (optional)    #
#-----------------------------#

# f_recover
# After a VIOS is booted, if hcheck_interval was 0 the Failed paths do NOT come
# back by themselves. This re-enables any Failed paths via chpath.
function f_recover {
    f_ensure_dirs
    v_rec="${v_basedir}/recover_${v_run_ts}.txt"
    : > "${v_rec}"
    f_log "INFO" "Checking for Failed MPIO paths to recover..."

    # list failed paths as: hdiskX <parent> <connection>
    v_failed=$(lspath -H -F "name path_id parent connection status" 2>/dev/null \
        | awk 'NR>1 && $NF ~ /Failed|Missing/ {print $1" "$3" "$4}')

    if [[ -z "${v_failed}" ]]; then
        f_log "INFO" "No Failed paths found. Nothing to recover."
        echo "No Failed paths."
        return 0
    fi

    echo "${v_failed}" | while read v_disk v_parent v_conn; do
        f_run "chpath -l ${v_disk} -p ${v_parent} -w '${v_conn}' -s enable" "${v_rec}" "Enable path ${v_disk} via ${v_parent}"
    done

    f_run "lspath" "${v_rec}" "MPIO path status after recovery"
    f_log "INFO" "Recovery attempt complete -> ${v_rec}"
    echo "Recovery log: ${v_rec}"
}

#-----------------------------#
# Package results (tar)       #
#-----------------------------#

# f_package
# Bundle the entire results folder into a single tar for easy transfer/report.
function f_package {
    f_ensure_dirs
    v_tar="/tmp/vios_res_${v_hostname}_${v_run_ts}.tar"
    v_parent=$(dirname "${v_basedir}")
    v_base=$(basename "${v_basedir}")

    f_log "INFO" "Packaging results into ${v_tar}"
    ( cd "${v_parent}" && tar -cf "${v_tar}" "${v_base}" ) 2>/dev/null

    # gzip if available (AIX usually has gzip)
    if command -v gzip >/dev/null 2>&1; then
        gzip -f "${v_tar}" 2>/dev/null && v_tar="${v_tar}.gz"
    fi

    f_log "INFO" "Package created: ${v_tar}"
    echo ""
    echo "=================================================================="
    echo " Results packaged: ${v_tar}"
    echo " Transfer this single file off the LPAR for your report."
    echo "=================================================================="
}

#-----------------------------#
# Usage + main dispatcher     #
#-----------------------------#
function f_usage {

    echo "vios_res_client.sh  (version ${v_version})"
    echo ""
    echo "Usage:"
    echo "  vios_res_client.sh discover                        Auto-discover devices -> inventory file"
    echo "  vios_res_client.sh adapters                        lscfg location codes + virtual slot map (fcs*/ent*)"
    echo "  vios_res_client.sh snapshot <label>                One-shot state capture (e.g. before/after)"
    echo "  vios_res_client.sh monitor <seconds>               Continuous ping + disk I/O monitor"
    echo "  vios_res_client.sh test <label> shutdown <seconds> Full flow: before -> monitor -> after_shutdown"
    echo "  vios_res_client.sh test <label> restart [timeout]  Full flow: before -> monitor -> wait VIOS up -> after_restart"
    echo "  vios_res_client.sh recover                         Re-enable any Failed MPIO paths (chpath enable)"
    echo "  vios_res_client.sh package                         Tar the results folder for transfer/report"
    echo "  vios_res_client.sh netsetup                        Setup per-interface ping targets (multi-network)"
    echo ""
    echo "Test modes (VIOS state is confirmed by YOU, not by ping - the VIOS may"
    echo "not be pingable):"
    echo "  shutdown - Immediately asks 'have you shut down the VIOS?'. As soon as you"
    echo "             confirm 'yes', it captures the down-state evidence right away"
    echo "             (cfgmgr + I/O probe to mark the dead path Failed, a short"
    echo "             ping/disk monitor burst of <seconds>, then the after_shutdown"
    echo "             snapshot). It then asks 'has the VIOS been started up again?'."
    echo "             As soon as you confirm 'yes', it captures the recovery evidence"
    echo "             (errpt/lspath/chpath + after_restart snapshot), packages the"
    echo "             results, and tells you to download the .tar.gz from the GUI."
    echo "             Snapshots: snapshot_<label>_after_shutdown_<timestamp>.txt"
    echo "                        snapshot_<label>_after_restart_<timestamp>.txt"
    echo "  restart  - Monitor for <seconds>. You confirm when the VIOS is DOWN"
    echo "             (cfgmgr + lspath -> 'during' snapshot), then confirm when it is"
    echo "             UP again. On recovery the script checks errpt for path-recovery"
    echo "             entries and lspath, and runs chpath to re-enable any still-Failed"
    echo "             paths, then captures the after-snapshot."
    echo "             Snapshot name: snapshot_<label>_after_restart_<timestamp>.txt"


    echo ""
    echo "Adapter location codes / virtual slots:"
    echo "  'adapters' runs lscfg on every fcsX (FC/NPIV) and entX (virtual ethernet)"
    echo "  adapter and decodes the location code:"
    echo "    U8284.22A.21ABCDE-V3-C12-T1"
    echo "                      |  |   \\_ T1 = port on the adapter"
    echo "                      |  \\____ C12 = the LPAR's VIRTUAL ADAPTER SLOT"
    echo "                      \\_______ V3  = LPAR (partition) id"
    echo "  Use the C<slot> value on the HMC to see which VIOS serves that adapter."
    echo "  Adapters in different slots = different VIOS = dual-VIOS redundancy."
    echo ""
    echo "Options:"
    echo "  -t <ip>    ping target (default: auto-detected default gateway)"
    echo "  -V <ip>    VIOS IP for restart detection (default: resolve from label)"
    echo "  -h         this help"
    echo ""
    echo "Multi-network ping:"
    echo "  The script can ping a target IP for EACH network interface during monitoring."
    echo "  Configure targets with:  ./vios_res_client.sh netsetup"
    echo "  Or manually create ${v_net_targets_file} with format:"
    echo "    en0=10.1.1.1"
    echo "    en1=10.2.2.1"
    echo "  If no config file exists, only the default gateway is pinged."
    echo ""
    echo "Examples:"
    echo "  ./vios_res_client.sh test vios1 shutdown 180   # shutdown test, 180s monitor"
    echo "  ./vios_res_client.sh test vios1 restart 180    # restart test, 180s monitor, then wait"
    echo "  ./vios_res_client.sh -V 10.0.0.5 test vios1 restart 180  # explicit VIOS IP"
    echo ""
    echo "Results are written under: ${v_basedir}"
}

# parse -t / -V / -h before positional args
v_vios_ip=""
while getopts "t:V:h" v_opt; do
    case ${v_opt} in
        t) v_ping_target="${OPTARG}" ;;
        V) v_vios_ip="${OPTARG}" ;;
        h) f_usage; exit 0 ;;
        *) f_usage; exit 2 ;;
    esac
done
shift $((OPTIND - 1))

v_action="$1"
f_ensure_dirs
echo "vios_res_client on ${v_hostname} | version ${v_version} | $(date)"

case "${v_action}" in
    discover)  f_discover ;;
    adapters)  f_adapter_slots; echo ""; cat "${v_basedir}/adapter_slots_${v_run_ts}.txt" ;;
    snapshot)  f_snapshot "$2" ;;
    monitor)   f_monitor "$2" ;;
    test)      f_test "$2" "$3" "$4" ;;
    recover)   f_recover ;;
    package)   f_package ;;
    netsetup)  f_setup_net_targets ;;
    ""|help|-h) f_usage ;;

    *) echo "Unknown action: ${v_action}"; f_usage; exit 2 ;;
esac
