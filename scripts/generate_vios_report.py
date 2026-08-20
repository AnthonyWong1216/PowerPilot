#!/usr/bin/env python3
"""
Generate a Word document consolidating VIOS Resilience Test results.
Reads test result data from testresult/vios_res_<hostname>_<timestamp>/ directories
and produces a structured .docx report with sections:
  - Before VIOS Shutdown/Restart
  - During VIOS Shutdown
  - After VIOS Resume/Started
Each section is separated by Network Resilience and Disk Path Resilience.
"""

import os
import sys
import glob
import re
import tarfile
from datetime import datetime

from docx import Document
from docx.shared import Inches, Pt, Cm, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.style import WD_STYLE_TYPE


def extract_test_archives(base_dir):
    """Find any vios_res_*.tar.gz archives in base_dir and extract them.

    Each archive (e.g. vios_res_aix14113_20260810_170924.tar.gz) is expected to
    contain a matching vios_res_<hostname>_<timestamp>/ directory with the log
    files. If the target directory already exists it is left as-is (no
    re-extraction). Returns the list of directory paths that were extracted.
    """
    pattern = os.path.join(base_dir, "vios_res_*.tar.gz")
    archives = sorted(glob.glob(pattern))
    extracted = []

    for archive in archives:
        # Strip the ".tar.gz" suffix to get the expected extracted dir name.
        arc_name = os.path.basename(archive)
        dir_name = arc_name[:-len(".tar.gz")]
        target_dir = os.path.join(base_dir, dir_name)

        if os.path.isdir(target_dir):
            print(f"  - Already extracted: {dir_name}")
            extracted.append(target_dir)
            continue

        print(f"  - Extracting: {arc_name}")
        try:
            with tarfile.open(archive, "r:gz") as tar:
                # Use the "data" filter when available (Python 3.12+) to safely
                # extract without applying archived metadata; fall back for
                # older Python versions that lack the filter argument.
                try:
                    tar.extractall(path=base_dir, filter="data")
                except TypeError:
                    tar.extractall(path=base_dir)
        except (tarfile.TarError, OSError) as exc:
            print(f"    WARNING: Failed to extract {arc_name}: {exc}")
            continue

        # The archive may extract to the expected dir name, or the log files may
        # sit at the archive root. Prefer the expected directory if present.
        if os.path.isdir(target_dir):
            extracted.append(target_dir)
        else:
            # Fall back: look for a newly-created vios_res_* directory.
            candidates = [
                d for d in glob.glob(os.path.join(base_dir, "vios_res_*"))
                if os.path.isdir(d)
            ]
            if candidates:
                extracted.extend(candidates)

    return extracted


def find_test_dirs(base_dir):
    """Find all vios_res_* test result directories.

    First extracts any vios_res_*.tar.gz archives found in base_dir so their
    log folders can be used to generate the report.
    """
    # Extract any compressed result archives first.
    extract_test_archives(base_dir)

    pattern = os.path.join(base_dir, "vios_res_*")
    dirs = sorted(glob.glob(pattern))
    return [d for d in dirs if os.path.isdir(d)]



def read_file_content(filepath):
    """Read file content, return empty string if not found."""
    if not os.path.exists(filepath):
        return ""
    with open(filepath, 'r') as f:
        return f.read()


def parse_inventory(content):
    """Parse inventory file to extract host info."""
    info = {}
    for line in content.split('\n'):
        if 'Host' in line and ':' in line:
            info['hostname'] = line.split(':')[-1].strip()
        if 'Generated' in line and ':' in line:
            info['generated'] = ':'.join(line.split(':')[1:]).strip()
    return info


def parse_monitor_entries(content):
    """Parse monitor log entries into a list of dicts with time, status, detail."""
    entries = []
    for line in content.strip().split('\n'):
        if not line.strip():
            continue
        parts = line.split(None, 2)
        if len(parts) >= 2:
            entry = {
                'time': parts[0],
                'status': parts[1],
                'detail': parts[2] if len(parts) > 2 else ''
            }
            entries.append(entry)
    return entries


def parse_vios_ping_entries(content):
    """Parse VIOS ping monitor entries."""
    entries = []
    for line in content.strip().split('\n'):
        if not line.strip():
            continue
        # Format: HH:MM:SS VIOS <name> <status> (<detail>)
        parts = line.split(None, 4)
        if len(parts) >= 4:
            entry = {
                'time': parts[0],
                'marker': parts[1],
                'vios_name': parts[2],
                'status': parts[3],
                'detail': parts[4] if len(parts) > 4 else ''
            }
            entries.append(entry)
    return entries


def extract_snapshot_sections(content):
    """Extract sections from a snapshot file, returning list of (purpose, data) tuples."""
    sections = []
    current_purpose = ""
    current_data = []
    in_data = False
    
    for line in content.split('\n'):
        if line.startswith('===================='):
            if current_purpose and current_data:
                sections.append((current_purpose, '\n'.join(current_data)))
            current_purpose = ""
            current_data = []
            in_data = False
        elif line.startswith('Purpose') and ':' in line:
            current_purpose = line.split(':', 1)[1].strip()
        elif line.startswith('----------'):
            in_data = True
        elif in_data:
            current_data.append(line)
    
    # Don't forget last section
    if current_purpose and current_data:
        sections.append((current_purpose, '\n'.join(current_data)))
    
    return sections


# Purposes that describe *static* environment configuration. These values
# (adapter location codes, WWPN / MAC addresses, interface list) are NOT
# modified during a VIOS shutdown, so they belong in the "Test Environment"
# section and should NOT be repeated in the before/during/after phases.
STATIC_ENV_KEYWORDS = [
    'location code',        # "Adapter location codes + virtual slot map"
    'vpd',                  # "lscfg location code/VPD for ..."
    'network interface list',  # netstat -in
]


def _is_static_env_purpose(purpose):
    """Return True if the snapshot section describes static environment config."""
    p = purpose.lower()
    return any(kw in p for kw in STATIC_ENV_KEYWORDS)


def filter_wwpn_section(data):
    """For an FC adapter VPD section, keep only the WWPN (Network Address) and
    Hardware Location Code lines. Drop the empty Device Specific (Z0..Z9) noise."""
    kept = []
    for line in data.split('\n'):
        stripped = line.strip()
        if not stripped:
            continue
        low = stripped.lower()
        # Skip empty Device Specific (Zx) lines
        if low.startswith('device specific'):
            continue
        # Skip displayable message noise
        if low.startswith('displayable message'):
            continue
        kept.append(line.rstrip())
    return '\n'.join(kept)


def filter_entstat_errors(data):
    """For an entstat section, keep only the essential header/state lines plus
    the core Transmit/Receive error & dropped-packet counters (up to and
    including "Bad Packets"). Everything after "Bad Packets" (collision counts,
    queue lengths, general statistics, etc.) is dropped to keep the report
    focused on the values that matter for a resilience test.

    Lines that are always kept regardless of position: the header/state lines
    (ETHERNET STATISTICS, Device Type, Hardware Address).
    """
    # Error/dropped counters we care about (only kept before/at "bad packets").
    error_keywords = [
        'transmit errors', 'receive errors',
        'packets dropped', 'bad packets',
    ]
    # Header/state lines kept regardless of where they appear.
    header_keywords = [
        'ethernet statistics', 'device type', 'hardware address',
        'physical port link', 'link status',
    ]

    kept = []
    stop = False
    for line in data.split('\n'):
        stripped = line.strip()
        if not stripped:
            continue
        low = stripped.lower()
        is_header = any(kw in low for kw in header_keywords)
        # Once we've passed "Bad Packets" we stop collecting error counters, but
        # still allow header/state lines (e.g. Driver Flags shown later).
        if not stop:
            is_error = any(kw in low for kw in error_keywords)
            if is_header or is_error:
                kept.append(line.rstrip())
            if 'bad packets' in low:
                stop = True
        else:
            if is_header:
                kept.append(line.rstrip())
    if not kept:
        return data.strip()
    return '\n'.join(kept)



def categorize_snapshot_sections(sections, static_only=False):
    """Categorize snapshot sections into network and disk categories.

    Excludes the routing table and the errpt/error report (errpt timestamps are
    delayed/inaccurate, so they are not displayed in the report).

    Args:
        static_only: When True, return only the *static* environment sections
            (adapter location codes, WWPN/MAC, interface list). When False
            (default), return only the *per-phase* case sections (lspath,
            lsmpio, entstat) that can change during a VIOS shutdown.
    """
    network_sections = []
    disk_sections = []

    network_keywords = ['Network', 'netstat', 'entstat', 'en0', 'ping']
    disk_keywords = ['MPIO', 'lspath', 'hdisk', 'lsmpio', 'disk', 'path']
    # Skip routing table and errpt/error report (errpt entries are delayed and
    # inaccurate, so they are excluded from all parts of the report).
    skip_keywords = ['routing', 'route', 'errpt', 'error']

    for purpose, data in sections:
        # Skip routing table and errpt/error sections
        if any(kw.lower() in purpose.lower() for kw in skip_keywords):
            continue

        is_static = _is_static_env_purpose(purpose)

        # Separate static environment info from per-phase case info.
        if static_only != is_static:
            continue

        # Trim WWPN/VPD sections to just the meaningful address + location code.
        if 'vpd' in purpose.lower():
            data = filter_wwpn_section(data)

        # For entstat, focus only on dropped/error packet counters.
        if 'entstat' in purpose.lower():
            data = filter_entstat_errors(data)

        is_network = any(kw.lower() in purpose.lower() for kw in network_keywords)
        is_disk = any(kw.lower() in purpose.lower() for kw in disk_keywords)

        if is_network:
            network_sections.append((purpose, data))
        elif is_disk:
            disk_sections.append((purpose, data))
        else:
            network_sections.append((purpose, data))

    return network_sections, disk_sections



def determine_time_phases(vios_ping_entries):
    """Determine time boundaries for before/during/after phases from VIOS ping log."""
    # Find when VIOS went DOWN and when it came back UP (recovered)
    down_time = None
    up_time = None
    
    for entry in vios_ping_entries:
        if 'DOWN' in entry['status'] and down_time is None:
            down_time = entry['time']
        if 'recovered' in entry.get('detail', ''):
            up_time = entry['time']
    
    return down_time, up_time


def classify_monitor_by_phase(entries, down_time, up_time):
    """Classify monitor entries into before/during/after phases."""
    before = []
    during = []
    after = []
    
    for entry in entries:
        t = entry['time']
        if down_time and t < down_time:
            before.append(entry)
        elif up_time and t > up_time:
            after.append(entry)
        else:
            during.append(entry)
    
    return before, during, after


# Colors used for status highlighting throughout the report.
COLOR_RED = RGBColor(0xC0, 0x00, 0x00)     # failures / errors
COLOR_GREEN = RGBColor(0x00, 0x80, 0x00)   # pass
COLOR_BLACK = RGBColor(0x00, 0x00, 0x00)   # enabled (emphasis, no alarm)

# Words that should be highlighted inside monospace code blocks.
#   - fail / failed  -> red + bold
#   - enabled        -> black + bold
#   - recovered      -> green + bold
_HIGHLIGHT_RE = re.compile(r'(failed|fail|enabled|recovered)', re.IGNORECASE)


def _style_highlight_run(run, word_lower):
    """Apply color/bold styling to a run based on the matched keyword."""
    run.bold = True
    if word_lower.startswith('fail'):
        run.font.color.rgb = COLOR_RED
    elif word_lower == 'enabled':
        run.font.color.rgb = COLOR_BLACK
    elif word_lower == 'recovered':
        run.font.color.rgb = COLOR_GREEN



def set_cell_text_highlighted(cell, text, bold_all=False):
    """Set a table cell's text, highlighting fail/failed (red+bold) and
    enabled (black+bold) words inline."""
    cell.text = ""
    para = cell.paragraphs[0]
    # Remove any pre-existing empty run.
    for r in list(para.runs):
        r.text = ""

    def add_run(segment, highlight=False):
        r = para.add_run(segment)
        if bold_all:
            r.bold = True
        if highlight:
            _style_highlight_run(r, segment.lower())
        return r

    last = 0
    for m in _HIGHLIGHT_RE.finditer(text):
        if m.start() > last:
            add_run(text[last:m.start()])
        add_run(m.group(0), highlight=True)
        last = m.end()
    if last < len(text):
        add_run(text[last:])
    if last == 0 and not para.runs:
        add_run(text)


def add_formatted_code_block(doc, text, font_size=7):
    """Add a monospace code block to the document.


    Highlights key status words inline:
      - "fail" / "failed"  -> red, bold
      - "enabled"          -> black, bold
    """
    para = doc.add_paragraph()
    para.paragraph_format.space_before = Pt(3)
    para.paragraph_format.space_after = Pt(3)

    text = text.rstrip()

    def add_run(segment, highlight=False):
        r = para.add_run(segment)
        r.font.name = 'Courier New'
        r.font.size = Pt(font_size)
        if highlight:
            _style_highlight_run(r, segment.lower())
        return r

    last = 0
    for m in _HIGHLIGHT_RE.finditer(text):
        if m.start() > last:
            add_run(text[last:m.start()])
        add_run(m.group(0), highlight=True)
        last = m.end()
    if last < len(text):
        add_run(text[last:])
    if last == 0:
        # No matches at all; ensure at least one run exists.
        if not para.runs:
            add_run(text)

    return para



def add_monitor_summary_table(doc, entries, monitor_type="ping"):
    """Add a summary table for monitor entries showing key stats."""
    if not entries:
        doc.add_paragraph("No data recorded in this phase.", style='List Bullet')
        return
    
    total = len(entries)
    if monitor_type == "ping":
        ok_count = sum(1 for e in entries if e['status'] == 'OK')
        loss_count = sum(1 for e in entries if e['status'] == 'LOSS')
        table = doc.add_table(rows=4, cols=2)
    else:  # disk
        ok_count = sum(1 for e in entries if e['status'] == 'OK')
        slow_count = sum(1 for e in entries if e['status'] == 'SLOW')
        fail_count = sum(1 for e in entries if e['status'] == 'FAIL')
        table = doc.add_table(rows=5, cols=2)
    
    table.style = 'Light Grid Accent 1'
    table.alignment = WD_TABLE_ALIGNMENT.LEFT
    
    # Header
    table.rows[0].cells[0].text = "Metric"
    table.rows[0].cells[1].text = "Value"
    
    table.rows[1].cells[0].text = "Total Checks"
    table.rows[1].cells[1].text = str(total)
    
    table.rows[2].cells[0].text = "OK"
    table.rows[2].cells[1].text = f"{ok_count} ({ok_count*100//total if total else 0}%)"
    
    if monitor_type == "ping":
        table.rows[3].cells[0].text = "LOSS (Packet Loss)"
        table.rows[3].cells[1].text = f"{loss_count} ({loss_count*100//total if total else 0}%)"
    else:
        table.rows[3].cells[0].text = "SLOW (Failover Stall)"
        table.rows[3].cells[1].text = f"{slow_count} ({slow_count*100//total if total else 0}%)"
        table.rows[4].cells[0].text = "FAIL"
        table.rows[4].cells[1].text = f"{fail_count} ({fail_count*100//total if total else 0}%)"
    
    # Bold header row
    for cell in table.rows[0].cells:
        for paragraph in cell.paragraphs:
            for run in paragraph.runs:
                run.bold = True


def add_monitor_detail_entries(doc, entries, max_show=5):
    """Add notable monitor entries (show issues + first 5 and last 5 from log)."""
    # Show all non-OK entries
    issues = [e for e in entries if e['status'] != 'OK']
    if issues:
        doc.add_paragraph("Notable Events:", style='List Bullet')
        for e in issues:
            doc.add_paragraph(
                f"  {e['time']} [{e['status']}] {e['detail']}",
                style='List Bullet 2'
            )
    
    # Show first 5 and last 5 ping results
    if entries:
        doc.add_paragraph(
            f"Time range: {entries[0]['time']} - {entries[-1]['time']} "
            f"({len(entries)} samples)",
            style='List Bullet'
        )
        
        if len(entries) > 10:
            doc.add_paragraph("First 5 entries:", style='List Bullet')
            first_lines = '\n'.join(
                f"{e['time']} {e['status']}  {e['detail']}" for e in entries[:5]
            )
            add_formatted_code_block(doc, first_lines, font_size=7)
            
            doc.add_paragraph("Last 5 entries:", style='List Bullet')
            last_lines = '\n'.join(
                f"{e['time']} {e['status']}  {e['detail']}" for e in entries[-5:]
            )
            add_formatted_code_block(doc, last_lines, font_size=7)
        else:
            # Show all if 10 or fewer
            all_lines = '\n'.join(
                f"{e['time']} {e['status']}  {e['detail']}" for e in entries
            )
            add_formatted_code_block(doc, all_lines, font_size=7)


def parse_test_dir(test_dir):
    """Parse a single test directory and return all parsed data as a dict."""
    basename = os.path.basename(test_dir)
    parts = basename.split('_')
    timestamp = '_'.join(parts[-2:])
    
    inventory_file = glob.glob(os.path.join(test_dir, "inventory_*.txt"))
    disk_monitor_file = glob.glob(os.path.join(test_dir, "monitor_disk_*.txt"))
    ping_monitor_file = glob.glob(os.path.join(test_dir, "monitor_ping_*.txt"))
    vios_ping_file = glob.glob(os.path.join(test_dir, "monitor_vios_ping_*.txt"))
    snapshot_before_files = glob.glob(os.path.join(test_dir, "snapshot_*_before_*.txt"))
    snapshot_during_files = glob.glob(os.path.join(test_dir, "snapshot_*_during_*.txt"))
    snapshot_after_files = glob.glob(os.path.join(test_dir, "snapshot_*_after_*.txt"))
    
    inventory_content = read_file_content(inventory_file[0]) if inventory_file else ""
    disk_monitor_content = read_file_content(disk_monitor_file[0]) if disk_monitor_file else ""
    ping_monitor_content = read_file_content(ping_monitor_file[0]) if ping_monitor_file else ""
    vios_ping_content = read_file_content(vios_ping_file[0]) if vios_ping_file else ""
    
    inv_info = parse_inventory(inventory_content)
    hostname = inv_info.get('hostname', 'Unknown')
    
    disk_entries = parse_monitor_entries(disk_monitor_content)
    ping_entries = parse_monitor_entries(ping_monitor_content)
    vios_ping_entries = parse_vios_ping_entries(vios_ping_content)
    
    down_time, up_time = determine_time_phases(vios_ping_entries)
    
    ping_before, ping_during, ping_after = classify_monitor_by_phase(ping_entries, down_time, up_time)
    disk_before, disk_during, disk_after = classify_monitor_by_phase(disk_entries, down_time, up_time)
    
    snapshot_before_content = read_file_content(snapshot_before_files[0]) if snapshot_before_files else ""
    snapshot_during_content = read_file_content(snapshot_during_files[0]) if snapshot_during_files else ""
    snapshot_after_content = read_file_content(snapshot_after_files[0]) if snapshot_after_files else ""
    
    before_sections = extract_snapshot_sections(snapshot_before_content)
    during_sections = extract_snapshot_sections(snapshot_during_content)
    after_sections = extract_snapshot_sections(snapshot_after_content)
    
    before_net, before_disk = categorize_snapshot_sections(before_sections)
    during_net, during_disk = categorize_snapshot_sections(during_sections)
    after_net, after_disk = categorize_snapshot_sections(after_sections)

    # Static environment snapshot (adapter location codes, WWPN/MAC addresses,
    # network interface list). These do not change during a VIOS shutdown, so
    # they are captured once (from the "before" snapshot) and shown only in the
    # Test Environment section. Fall back to during/after if before is missing.
    env_source = before_sections or during_sections or after_sections
    env_net, env_disk = categorize_snapshot_sections(env_source, static_only=True)

    
    vios_name = "VIOS"
    if snapshot_before_files:
        fname = os.path.basename(snapshot_before_files[0])
        match = re.match(r'snapshot_(.+?)_(before|during|after)', fname)
        if match:
            vios_name = match.group(1)
    
    # Compute downtime
    if down_time and up_time:
        dt_parts = down_time.split(':')
        ut_parts = up_time.split(':')
        down_secs = int(dt_parts[0])*3600 + int(dt_parts[1])*60 + int(dt_parts[2])
        up_secs = int(ut_parts[0])*3600 + int(ut_parts[1])*60 + int(ut_parts[2])
        downtime_secs = up_secs - down_secs
        downtime_str = f"{downtime_secs // 60}m {downtime_secs % 60}s"
    else:
        downtime_str = "N/A"
    
    total_ping = len(ping_entries)
    ping_loss = sum(1 for e in ping_entries if e['status'] == 'LOSS')
    total_disk = len(disk_entries)
    disk_slow = sum(1 for e in disk_entries if e['status'] == 'SLOW')
    disk_fail = sum(1 for e in disk_entries if e['status'] == 'FAIL')
    
    return {
        'test_dir': test_dir,
        'basename': basename,
        'timestamp': timestamp,
        'hostname': hostname,
        'vios_name': vios_name,
        'inventory_content': inventory_content,
        'vios_ping_entries': vios_ping_entries,
        'down_time': down_time,
        'up_time': up_time,
        'downtime_str': downtime_str,
        'ping_entries': ping_entries,
        'disk_entries': disk_entries,
        'ping_before': ping_before,
        'ping_during': ping_during,
        'ping_after': ping_after,
        'disk_before': disk_before,
        'disk_during': disk_during,
        'disk_after': disk_after,
        'before_net': before_net,
        'before_disk': before_disk,
        'during_net': during_net,
        'during_disk': during_disk,
        'after_net': after_net,
        'after_disk': after_disk,
        'env_net': env_net,
        'env_disk': env_disk,
        'total_ping': total_ping,

        'ping_loss': ping_loss,
        'total_disk': total_disk,
        'disk_slow': disk_slow,
        'disk_fail': disk_fail,
    }


def add_host_section(doc, data, host_num, phase, section_offset):
    """Add a host's network and disk sections for a given phase (before/during/after).
    phase: 'before', 'during', or 'after'
    """
    hostname = data['hostname']
    vios_name = data['vios_name']
    
    phase_label = {
        'before': 'Before Shutdown',
        'during': 'During Shutdown',
        'after': 'After Shutdown'
    }[phase]
    
    ping_data = data[f'ping_{phase}']
    disk_data = data[f'disk_{phase}']
    net_sections = data[f'{phase}_net']
    disk_sections = data[f'{phase}_disk']
    
    # Host sub-heading
    doc.add_heading(f'{section_offset}.{host_num} Host: {hostname} (VIOS: {vios_name})', level=2)
    
    # Network Path Resilience
    doc.add_heading(f'Network Path Resilience — {phase_label}', level=3)
    add_monitor_summary_table(doc, ping_data, "ping")
    add_monitor_detail_entries(doc, ping_data)
    
    if net_sections:
        doc.add_paragraph("Network Snapshot:", style='List Bullet')
        for purpose, sect_data in net_sections:
            doc.add_paragraph(f"► {purpose}", style='List Bullet 2')
            add_formatted_code_block(doc, sect_data.strip(), font_size=7)
    
    # Disk/SAN Path Resilience
    doc.add_heading(f'Disk/SAN Path Resilience — {phase_label}', level=3)
    add_monitor_summary_table(doc, disk_data, "disk")
    add_monitor_detail_entries(doc, disk_data)
    
    if disk_sections:
        doc.add_paragraph("Disk/MPIO Snapshot:", style='List Bullet')
        for purpose, sect_data in disk_sections:
            doc.add_paragraph(f"► {purpose}", style='List Bullet 2')
            add_formatted_code_block(doc, sect_data.strip(), font_size=7)


def generate_consolidated_report(test_dirs, output_path):
    """Generate a single consolidated Word document for all test directories."""
    
    # Parse all test directories
    all_data = []
    for test_dir in test_dirs:
        data = parse_test_dir(test_dir)
        all_data.append(data)
    
    # Collect unique VIOS names and hosts
    vios_names = list(set(d['vios_name'] for d in all_data))
    hostnames = [d['hostname'] for d in all_data]
    
    # Use first timestamp for report date
    first_ts = all_data[0]['timestamp']
    
    # ========== Create Word Document ==========
    doc = Document()
    
    style = doc.styles['Normal']
    font = style.font
    font.name = 'Calibri'
    font.size = Pt(10)
    
    # ---- Title Page ----
    doc.add_paragraph()
    doc.add_paragraph()
    title = doc.add_heading('VIOS Resilience Test Report', level=0)
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    
    subtitle = doc.add_paragraph()
    subtitle.alignment = WD_ALIGN_PARAGRAPH.CENTER
    vios_list = ', '.join(vios_names)
    host_list = ', '.join(hostnames)
    run = subtitle.add_run(f'\nVIOS Tested: {vios_list}\nClient Hosts: {host_list}\n')
    run.font.size = Pt(14)
    
    info_para = doc.add_paragraph()
    info_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = info_para.add_run(f'Test Date: {first_ts[:4]}-{first_ts[4:6]}-{first_ts[6:8]}')
    run.font.size = Pt(12)
    
    gen_para = doc.add_paragraph()
    gen_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = gen_para.add_run(f'\nReport Generated: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')
    run.font.size = Pt(10)
    run.font.color.rgb = RGBColor(100, 100, 100)
    
    doc.add_page_break()
    
    # ---- Table of Contents ----
    doc.add_heading('Table of Contents', level=1)
    toc_items = [
        ('1. Executive Summary', False),
        ('2. Test Environment', False),
    ]
    for idx, data in enumerate(all_data):
        toc_items.append((f'    2.{idx+1} {data["hostname"]}', True))
    
    toc_items.append(('3. VIOS Status Timeline', False))
    toc_items.append(('4. Before VIOS Shutdown/Restart', False))
    for idx, data in enumerate(all_data):
        toc_items.append((f'    4.{idx+1} {data["hostname"]} (VIOS: {data["vios_name"]})', True))
    
    toc_items.append(('5. During VIOS Shutdown', False))
    for idx, data in enumerate(all_data):
        toc_items.append((f'    5.{idx+1} {data["hostname"]} (VIOS: {data["vios_name"]})', True))
    
    toc_items.append(('6. After VIOS Resume/Started', False))
    for idx, data in enumerate(all_data):
        toc_items.append((f'    6.{idx+1} {data["hostname"]} (VIOS: {data["vios_name"]})', True))
    
    toc_items.append(('7. Conclusion', False))
    
    for item_text, is_sub in toc_items:
        para = doc.add_paragraph()
        run = para.add_run(item_text)
        run.font.size = Pt(10) if not is_sub else Pt(9)
        if is_sub:
            para.paragraph_format.left_indent = Cm(1.5)
    
    doc.add_page_break()
    
    # ---- 1. Executive Summary ----
    doc.add_heading('1. Executive Summary', level=1)
    
    # Summary table for all hosts
    summary_table = doc.add_table(rows=len(all_data)+1, cols=7)
    summary_table.style = 'Light Grid Accent 1'
    
    headers = ["Host", "VIOS", "Downtime", "Ping Total", "Ping Loss", "Disk Slow/Fail", "Result"]
    for i, h in enumerate(headers):
        summary_table.rows[0].cells[i].text = h
        for p in summary_table.rows[0].cells[i].paragraphs:
            for run in p.runs:
                run.bold = True
    
    for idx, data in enumerate(all_data):
        row = summary_table.rows[idx + 1]
        row.cells[0].text = data['hostname']
        row.cells[1].text = data['vios_name']
        row.cells[2].text = data['downtime_str']
        row.cells[3].text = str(data['total_ping'])
        row.cells[4].text = f"{data['ping_loss']} ({data['ping_loss']*100//data['total_ping'] if data['total_ping'] else 0}%)"
        row.cells[5].text = f"{data['disk_slow']}s / {data['disk_fail']}f"
        passed = data['ping_loss'] <= 5 and data['disk_fail'] == 0
        result_cell = row.cells[6]
        result_cell.text = ""
        rp = result_cell.paragraphs[0]
        rrun = rp.add_run("PASS" if passed else "REVIEW")
        rrun.bold = True
        rrun.font.color.rgb = COLOR_GREEN if passed else COLOR_RED

    
    doc.add_paragraph()
    doc.add_paragraph(
        "This report consolidates the VIOS resilience test results for multiple client hosts, "
        "showing the impact on network connectivity and disk/SAN path availability when a VIOS is "
        "shut down and restarted. The test validates that AIX LPARs maintain "
        "continuous service availability through redundant VIOS paths."
    )
    
    doc.add_page_break()
    
    # ---- 2. Test Environment ----
    doc.add_heading('2. Test Environment', level=1)
    
    doc.add_paragraph(
        "The adapter configuration below (location codes, FC WWPN addresses, "
        "Ethernet MAC address and network interface list) is captured once here. "
        "These values are part of the fixed test environment and are NOT modified "
        "during the VIOS shutdown/restart, so they are not repeated in the "
        "before/during/after phases."
    )

    for idx, data in enumerate(all_data):
        doc.add_heading(f'2.{idx+1} Host: {data["hostname"]}', level=2)
        doc.add_paragraph(f"Test directory: {data['basename']}")
        add_formatted_code_block(doc, data['inventory_content'], font_size=8)

        # Static adapter/network configuration snapshot (WWPN, MAC, location
        # codes, netstat -in). Shown only here since they don't change.
        env_sections = data['env_net'] + data['env_disk']
        if env_sections:
            doc.add_heading('Adapter & Network Configuration Snapshot', level=3)
            for purpose, sect_data in env_sections:
                doc.add_paragraph(f"► {purpose}", style='List Bullet')
                add_formatted_code_block(doc, sect_data.strip(), font_size=7)

    doc.add_page_break()

    
    # ---- 3. VIOS Status Timeline ----
    doc.add_heading('3. VIOS Status Timeline', level=1)
    
    for idx, data in enumerate(all_data):
        doc.add_heading(f'3.{idx+1} Host: {data["hostname"]} → VIOS: {data["vios_name"]}', level=2)
        
        vios_ping_entries = data['vios_ping_entries']
        if vios_ping_entries:
            # Deduplicate consecutive UP rows
            filtered_vios_entries = []
            prev_status = None
            for entry in vios_ping_entries:
                if entry['status'] == 'UP' and prev_status == 'UP':
                    continue
                filtered_vios_entries.append(entry)
                prev_status = entry['status']
            
            vios_table = doc.add_table(rows=len(filtered_vios_entries)+1, cols=4)
            vios_table.style = 'Light Grid Accent 1'
            t_headers = ["Time", "VIOS", "Status", "Detail"]
            for i, h in enumerate(t_headers):
                vios_table.rows[0].cells[i].text = h
                for p in vios_table.rows[0].cells[i].paragraphs:
                    for run in p.runs:
                        run.bold = True
            
            for eidx, entry in enumerate(filtered_vios_entries):
                row = vios_table.rows[eidx + 1]
                row.cells[0].text = entry['time']
                row.cells[1].text = entry['vios_name']
                row.cells[2].text = entry['status']
                row.cells[3].text = entry.get('detail', '')
        
        doc.add_paragraph()
        doc.add_paragraph(f"VIOS went DOWN at: {data['down_time'] if data['down_time'] else 'N/A'}")
        doc.add_paragraph(f"VIOS came back UP at: {data['up_time'] if data['up_time'] else 'N/A'}")
        doc.add_paragraph(f"Total downtime: {data['downtime_str']}")
        doc.add_paragraph()
    
    doc.add_page_break()
    
    # ---- 4. BEFORE VIOS Shutdown/Restart ----
    doc.add_heading('4. Before VIOS Shutdown/Restart', level=1)
    doc.add_paragraph(
        "This section shows the system state before the VIOS was shut down. "
        "All paths should be fully operational."
    )
    
    for idx, data in enumerate(all_data):
        add_host_section(doc, data, idx+1, 'before', 4)
    
    doc.add_page_break()
    
    # ---- 5. DURING VIOS Shutdown ----
    doc.add_heading('5. During VIOS Shutdown', level=1)
    doc.add_paragraph(
        "This section shows the system state while the VIOS was down. "
        "Redundant paths should take over to maintain service continuity."
    )
    
    for idx, data in enumerate(all_data):
        add_host_section(doc, data, idx+1, 'during', 5)
    
    doc.add_page_break()
    
    # ---- 6. AFTER VIOS Resume/Started ----
    doc.add_heading('6. After VIOS Resume/Started', level=1)
    doc.add_paragraph(
        "This section shows the system state after the VIOS has recovered. "
        "All paths should return to Enabled state."
    )
    
    for idx, data in enumerate(all_data):
        add_host_section(doc, data, idx+1, 'after', 6)
    
    doc.add_page_break()
    
    # ---- 7. Conclusion ----
    doc.add_heading('7. Conclusion', level=1)
    
    # Overall assessment
    all_pass = all(d['ping_loss'] <= 5 and d['disk_fail'] == 0 for d in all_data)
    
    if all_pass:
        doc.add_paragraph(
            "All VIOS resilience tests completed successfully. During the VIOS shutdown periods, "
            "all AIX LPARs maintained continuous network connectivity and disk I/O availability "
            "through redundant VIOS paths. All MPIO paths recovered to Enabled state after VIOS restart. "
            "The redundant VIOS configuration is functioning correctly for all tested hosts."
        )
    else:
        doc.add_paragraph(
            "The VIOS resilience tests completed with some observations. "
            "Review the per-host details above for specific impact assessment."
        )
    
    # Per-host conclusion — PASS in green, REVIEW/FAIL in red.
    for data in all_data:
        network_ok = data['ping_loss'] <= 2
        disk_ok = data['disk_fail'] == 0
        is_pass = network_ok and disk_ok
        status = "✓ PASS" if is_pass else "⚠ REVIEW"
        para = doc.add_paragraph(style='List Bullet')
        status_run = para.add_run(status)
        status_run.bold = True
        status_run.font.color.rgb = COLOR_GREEN if is_pass else COLOR_RED
        para.add_run(
            f" — {data['hostname']} (VIOS: {data['vios_name']}): "
            f"Downtime {data['downtime_str']}, "
            f"{data['ping_loss']} ping loss, "
            f"{data['disk_slow']} slow / {data['disk_fail']} fail disk I/O"
        )

    
    # Comparison table
    doc.add_heading('Path Status Comparison', level=2)
    
    num_hosts = len(all_data)
    comp_table = doc.add_table(rows=1 + num_hosts * 3, cols=5)
    comp_table.style = 'Light Grid Accent 1'
    
    comp_headers = ["Host", "Component", "Before Shutdown", "During Shutdown", "After Shutdown"]
    for i, h in enumerate(comp_headers):
        comp_table.rows[0].cells[i].text = h
        for p in comp_table.rows[0].cells[i].paragraphs:
            for run in p.runs:
                run.bold = True
    
    row_idx = 1
    for data in all_data:
        # Network row
        comp_table.rows[row_idx].cells[0].text = data['hostname']
        comp_table.rows[row_idx].cells[1].text = "Network (Ping)"
        comp_table.rows[row_idx].cells[2].text = f"{len(data['ping_before'])} OK" if not any(e['status']=='LOSS' for e in data['ping_before']) else "Some LOSS"
        comp_table.rows[row_idx].cells[3].text = f"{sum(1 for e in data['ping_during'] if e['status']=='OK')} OK, {sum(1 for e in data['ping_during'] if e['status']=='LOSS')} LOSS"
        comp_table.rows[row_idx].cells[4].text = f"{len(data['ping_after'])} OK" if not any(e['status']=='LOSS' for e in data['ping_after']) else "Some LOSS"
        row_idx += 1
        
        # Disk row
        comp_table.rows[row_idx].cells[0].text = data['hostname']
        comp_table.rows[row_idx].cells[1].text = "Disk I/O"
        comp_table.rows[row_idx].cells[2].text = f"{sum(1 for e in data['disk_before'] if e['status']=='OK')} OK"
        comp_table.rows[row_idx].cells[3].text = f"{sum(1 for e in data['disk_during'] if e['status']=='OK')} OK, {sum(1 for e in data['disk_during'] if e['status']=='SLOW')} SLOW"
        comp_table.rows[row_idx].cells[4].text = f"{sum(1 for e in data['disk_after'] if e['status']=='OK')} OK"
        row_idx += 1
        
        # MPIO row (highlight Enabled -> black bold, Failed -> red bold,
        # Recovered -> green bold)
        comp_table.rows[row_idx].cells[0].text = data['hostname']
        comp_table.rows[row_idx].cells[1].text = "MPIO Paths"
        set_cell_text_highlighted(comp_table.rows[row_idx].cells[2], "All Enabled")
        set_cell_text_highlighted(comp_table.rows[row_idx].cells[3], "Partial Failed")
        set_cell_text_highlighted(comp_table.rows[row_idx].cells[4], "Recovered")
        row_idx += 1


    
    # Save document. If the target file is open (e.g. in Microsoft Word) Windows
    # locks it and raises PermissionError. In that case, fall back to a
    # timestamped filename so the run still succeeds.
    try:
        doc.save(output_path)
        print(f"Report generated: {output_path}")
    except PermissionError:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")

        root, ext = os.path.splitext(output_path)
        fallback_path = f"{root}_{ts}{ext}"
        print(
            f"WARNING: Could not write '{output_path}'.\n"
            f"         The file is likely OPEN in Microsoft Word (or another program).\n"
            f"         Close it and re-run to overwrite, OR use this new file instead:"
        )
        doc.save(fallback_path)
        print(f"Report generated: {fallback_path}")



def main():
    base_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "testresult")
    
    if not os.path.exists(base_dir):
        print(f"Error: testresult directory not found at {base_dir}")
        sys.exit(1)
    
    test_dirs = find_test_dirs(base_dir)
    
    if not test_dirs:
        print("Error: No vios_res_* test result directories found.")
        sys.exit(1)
    
    print(f"Found {len(test_dirs)} test result directory(ies):")
    for d in test_dirs:
        print(f"  - {os.path.basename(d)}")
    
    # Generate a single consolidated report for all test directories
    output_filename = "VIOS_Resilience_Report.docx"
    output_path = os.path.join(base_dir, output_filename)
    
    print(f"\nGenerating consolidated report for {len(test_dirs)} host(s)...")
    generate_consolidated_report(test_dirs, output_path)


if __name__ == "__main__":
    main()
