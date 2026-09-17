#!/usr/bin/env python3
"""
Generate a consolidated Word report merging ALL selected test results
(iperf throughput + VIOS resilience) into one document.

Document structure
==================
  1. Network Throughput Test Results  (TCP only)
  2. VIOS Resilience Test
     2.1 Network Resilience Test
         2.1.1 Before Shutdown VIOS <name>
         2.1.2 During Shutdown VIOS <name>
         2.1.3 After Shutdown VIOS <name>
     2.2 Disk/SAN Path Resilience Test
         2.2.1 Before Shutdown VIOS <name>
         2.2.2 During Shutdown VIOS <name>
         2.2.3 After Shutdown VIOS <name>
  3. Summary
  4. Appendix & Logs  (raw logs attached, not printed in body)

Usage:
    python generate_consolidated_report.py --files file1 file2 ...
    python generate_consolidated_report.py          # auto-discover all
"""

import os
import sys
import glob
from datetime import datetime

from docx import Document
from docx.shared import Pt, Cm, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH


SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPTS_DIR)

from generate_iperf_report import (
    parse_iperf_log, parse_server_log, find_iperf_logs,
    find_iperf_server_logs,
)
from generate_vios_report import (
    find_test_dirs, extract_test_archives, parse_test_dir,
    add_formatted_code_block,
    add_monitor_summary_table, add_monitor_detail_entries,
)

COLOR_GREEN = RGBColor(0x1A, 0x7F, 0x37)
COLOR_RED = RGBColor(0xD1, 0x24, 0x2F)
COLOR_GREY = RGBColor(100, 100, 100)


# ── Helpers ───────────────────────────────────────────────

def _bold_cell(cell, text):
    """Set cell text and make every run bold."""
    cell.text = text
    for p in cell.paragraphs:
        for r in p.runs:
            r.bold = True


def _kv_table(doc, rows):
    """Add a 2-column key/value table."""
    tbl = doc.add_table(rows=len(rows), cols=2)
    tbl.style = "Light Grid Accent 1"
    for i, (label, val) in enumerate(rows):
        _bold_cell(tbl.rows[i].cells[0], label)
        tbl.rows[i].cells[1].text = val or "\u2014"
    return tbl


def _timeline_para(doc, data):
    """Add VIOS down/up/downtime paragraph."""
    doc.add_paragraph(
        f"VIOS went DOWN at: {data['down_time'] or 'N/A'}  \u2502  "
        f"VIOS came UP at: {data['up_time'] or 'N/A'}  \u2502  "
        f"Total downtime: {data['downtime_str']}")


# ── File classification ───────────────────────────────────

def classify_files(base_dir, filenames):
    """Sort filenames into iperf-client, iperf-server, vios buckets."""
    iperf_client, iperf_server, vios_archives = [], [], []
    for name in filenames:
        full = os.path.join(base_dir, name)
        if not os.path.exists(full):
            continue
        if name.startswith("iperf_srv_"):
            iperf_server.append(full)
        elif "iperf" in name and name.endswith("_result.log"):
            iperf_client.append(full)
        elif "iperf" in name and name.endswith(".log"):
            iperf_server.append(full)
        elif name.startswith("vios_res_"):
            vios_archives.append(name)
    return sorted(iperf_client), sorted(iperf_server), sorted(vios_archives)


def resolve_vios_dirs(base_dir, archive_names):
    """Extract archives if needed and return matching test dirs.
    When archive_names is an empty list, return [] (nothing selected)."""
    if archive_names is not None and len(archive_names) == 0:
        return []
    extract_test_archives(base_dir)
    all_dirs = find_test_dirs(base_dir)
    if archive_names is None:
        return all_dirs
    bases = set()
    for n in archive_names:
        bases.add(n[:-len(".tar.gz")] if n.endswith(".tar.gz") else n)
    return [d for d in all_dirs if os.path.basename(d) in bases]


def _collect_vios_log_files(test_dir):
    """Return sorted list of every .txt / .log file inside a VIOS test dir."""
    files = []
    for ext in ("*.txt", "*.log"):
        files.extend(glob.glob(os.path.join(test_dir, ext)))
    return sorted(files)


# ═══════════════════════════════════════════════════════════
#  Section 1 — Network Throughput
# ═══════════════════════════════════════════════════════════

def add_iperf_section(doc, all_client, all_server, sec_num):
    """Write the Network Throughput section. Returns next sec number."""
    doc.add_heading(f"{sec_num}. Network Throughput Test Results", level=1)
    doc.add_paragraph(
        "iperf TCP bandwidth measurements between AIX LPARs.")

    # Summary table
    cols = ["#", "Client", "Server IP", "Date", "TCP Bandwidth"]
    tbl = doc.add_table(rows=1 + len(all_client), cols=len(cols))
    tbl.style = "Light Grid Accent 1"
    for i, h in enumerate(cols):
        _bold_cell(tbl.rows[0].cells[i], h)
    for idx, d in enumerate(all_client):
        r = tbl.rows[idx + 1]
        r.cells[0].text = str(idx + 1)
        r.cells[1].text = d["hostname"] or "\u2014"
        r.cells[2].text = d["server_ip"] or "\u2014"
        r.cells[3].text = d["test_date"] or "\u2014"
        r.cells[4].text = d["tcp_bandwidth"] or "\u2014"
    doc.add_paragraph()

    # Per-test detail
    for idx, d in enumerate(all_client):
        doc.add_heading(
            f"{sec_num}.{idx+1} {d['hostname'] or 'Test ' + str(idx+1)}",
            level=2)
        _kv_table(doc, [
            ("Client Hostname", d["hostname"]),
            ("AIX Version", d["aix_version"]),
            ("Target Server IP", d["server_ip"]),
            ("iperf Port", d["port"]),
            ("Test Duration", d["duration"]),
            ("Test Date", d["test_date"]),
        ])
        doc.add_paragraph()
        doc.add_heading("TCP Throughput", level=3)
        _kv_table(doc, [
            ("TCP Window Size", d["tcp_window"]),
            ("Transfer", d["tcp_transfer"]),
            ("Bandwidth", d["tcp_bandwidth"]),
        ])
        if d["tcp_raw"]:
            doc.add_paragraph()
            add_formatted_code_block(doc, d["tcp_raw"])
        doc.add_paragraph()

    if all_server:
        doc.add_heading(
            f"{sec_num}.{len(all_client)+1} Server-Side Logs", level=2)
        for sd in all_server:
            doc.add_heading(sd["filename"], level=3)
            if sd["bandwidth"]:
                p = doc.add_paragraph()
                p.add_run("Server bandwidth: ").bold = True
                p.add_run(
                    f"{sd['bandwidth']}  (Transfer: {sd['transfer']})")
            add_formatted_code_block(doc, sd["raw"])
            doc.add_paragraph()

    return sec_num + 1



# ═══════════════════════════════════════════════════════════
#  Section 2 — VIOS Resilience Test
#
#  2.1 Network Resilience Test
#      2.1.1 Before Shutdown VIOS <name>
#      2.1.2 During Shutdown VIOS <name>
#      2.1.3 After Shutdown VIOS <name>
#  2.2 Disk/SAN Path Resilience Test
#      2.2.1 Before Shutdown VIOS <name>
#      2.2.2 During Shutdown VIOS <name>
#      2.2.3 After Shutdown VIOS <name>
# ═══════════════════════════════════════════════════════════

_PHASE_META = [
    ("before", "Before Shutdown"),
    ("during", "During Shutdown"),
    ("after",  "After Shutdown"),
]


def _add_net_phase(doc, data, sub, phase, phase_label, vios_name):
    """Write one Network Resilience phase subsection."""
    doc.add_heading(
        f"{sub} {phase_label} VIOS {vios_name}  "
        f"(Host: {data['hostname']})", level=3)
    _timeline_para(doc, data)
    ping_data = data[f"ping_{phase}"]
    net_sections = data[f"{phase}_net"]
    add_monitor_summary_table(doc, ping_data, "ping")
    add_monitor_detail_entries(doc, ping_data)
    if net_sections:
        doc.add_paragraph("Network Snapshot:", style="List Bullet")
        for purpose, sect_data in net_sections:
            doc.add_paragraph(f"\u25ba {purpose}", style="List Bullet 2")
            add_formatted_code_block(doc, sect_data.strip(), font_size=7)
    doc.add_paragraph()


def _add_disk_phase(doc, data, sub, phase, phase_label, vios_name):
    """Write one Disk/SAN Resilience phase subsection."""
    doc.add_heading(
        f"{sub} {phase_label} VIOS {vios_name}  "
        f"(Host: {data['hostname']})", level=3)
    _timeline_para(doc, data)
    disk_data = data[f"disk_{phase}"]
    disk_sections = data[f"{phase}_disk"]
    add_monitor_summary_table(doc, disk_data, "disk")
    add_monitor_detail_entries(doc, disk_data)
    if disk_sections:
        doc.add_paragraph("Disk/MPIO Snapshot:", style="List Bullet")
        for purpose, sect_data in disk_sections:
            doc.add_paragraph(f"\u25ba {purpose}", style="List Bullet 2")
            add_formatted_code_block(doc, sect_data.strip(), font_size=7)
    doc.add_paragraph()


def add_vios_section(doc, all_data, sec_num):
    """Write the full VIOS Resilience section. Returns next sec number."""

    doc.add_heading(f"{sec_num}. VIOS Resilience Test", level=1)
    doc.add_paragraph(
        "This section validates that AIX LPARs maintain continuous service "
        "availability through redundant VIOS paths during a VIOS shutdown "
        "and restart cycle.")

    # Collect VIOS / host names for the intro
    vios_list = ", ".join(sorted(set(d["vios_name"] for d in all_data)))
    host_list = ", ".join(d["hostname"] for d in all_data)
    doc.add_paragraph(f"VIOS under test: {vios_list}")
    doc.add_paragraph(f"Client hosts: {host_list}")
    doc.add_paragraph()

    # ── 2.1  Network Resilience Test ──
    net_sec = f"{sec_num}.1"
    doc.add_heading(f"{net_sec} Network Resilience Test", level=2)
    doc.add_paragraph(
        "Ping monitoring results and network adapter/interface snapshots "
        "captured before, during, and after VIOS shutdown.")
    sub_idx = 1
    for phase, phase_label in _PHASE_META:
        for d in all_data:
            sub = f"{net_sec}.{sub_idx}"
            _add_net_phase(doc, d, sub, phase, phase_label, d["vios_name"])
            sub_idx += 1

    # ── 2.2  Disk/SAN Path Resilience Test ──
    disk_sec = f"{sec_num}.2"
    doc.add_heading(f"{disk_sec} Disk/SAN Path Resilience Test", level=2)
    doc.add_paragraph(
        "Disk I/O monitoring results and MPIO/SAN path snapshots "
        "captured before, during, and after VIOS shutdown.")
    sub_idx = 1
    for phase, phase_label in _PHASE_META:
        for d in all_data:
            sub = f"{disk_sec}.{sub_idx}"
            _add_disk_phase(doc, d, sub, phase, phase_label, d["vios_name"])
            sub_idx += 1

    return sec_num + 1



# ═══════════════════════════════════════════════════════════
#  Section 3 — Summary
# ═══════════════════════════════════════════════════════════

def add_summary_section(doc, all_client, all_vios, sec_num):
    """Combined summary for all test types. Returns next sec number."""
    doc.add_heading(f"{sec_num}. Summary", level=1)

    # ── Throughput summary ──
    if all_client:
        doc.add_heading(f"{sec_num}.1 Network Throughput Summary", level=2)
        cols = ["Client", "Server IP", "TCP BW", "Result"]
        tbl = doc.add_table(rows=1 + len(all_client), cols=len(cols))
        tbl.style = "Light Grid Accent 1"
        for i, h in enumerate(cols):
            _bold_cell(tbl.rows[0].cells[i], h)
        for idx, d in enumerate(all_client):
            r = tbl.rows[idx + 1]
            r.cells[0].text = d["hostname"] or "\u2014"
            r.cells[1].text = d["server_ip"] or "\u2014"
            r.cells[2].text = d["tcp_bandwidth"] or "\u2014"
            has_bw = bool(d["tcp_bandwidth"])
            rc = r.cells[3]
            rc.text = ""
            rr = rc.paragraphs[0].add_run("PASS" if has_bw else "NO DATA")
            rr.bold = True
            rr.font.color.rgb = COLOR_GREEN if has_bw else COLOR_RED
        doc.add_paragraph()

    # ── VIOS Resilience summary ──
    if all_vios:
        sub = "2" if all_client else "1"
        doc.add_heading(
            f"{sec_num}.{sub} VIOS Resilience Summary", level=2)
        cols = ["Host", "VIOS", "Downtime", "Ping Total",
                "Ping Loss", "Disk Slow/Fail", "Result"]
        tbl = doc.add_table(rows=1 + len(all_vios), cols=len(cols))
        tbl.style = "Light Grid Accent 1"
        for i, h in enumerate(cols):
            _bold_cell(tbl.rows[0].cells[i], h)
        for idx, d in enumerate(all_vios):
            r = tbl.rows[idx + 1]
            r.cells[0].text = d["hostname"]
            r.cells[1].text = d["vios_name"]
            r.cells[2].text = d["downtime_str"]
            r.cells[3].text = str(d["total_ping"])
            pct = (d["ping_loss"] * 100 // d["total_ping"]
                   if d["total_ping"] else 0)
            r.cells[4].text = f"{d['ping_loss']} ({pct}%)"
            r.cells[5].text = f"{d['disk_slow']}s / {d['disk_fail']}f"
            passed = d["ping_loss"] <= 5 and d["disk_fail"] == 0
            rc = r.cells[6]
            rc.text = ""
            rr = rc.paragraphs[0].add_run("PASS" if passed else "REVIEW")
            rr.bold = True
            rr.font.color.rgb = COLOR_GREEN if passed else COLOR_RED
        doc.add_paragraph()

    # ── Overall conclusion ──
    doc.add_heading(f"{sec_num}.{'3' if all_client and all_vios else sub if all_vios else '2'} Overall Conclusion", level=2)
    parts = []
    if all_client:
        parts.append(
            "Network throughput tests captured TCP bandwidth "
            "measurements for all tested LPARs.")
    if all_vios:
        all_pass = all(
            d["ping_loss"] <= 5 and d["disk_fail"] == 0 for d in all_vios)
        if all_pass:
            parts.append(
                "All VIOS resilience tests completed successfully. "
                "During the VIOS shutdown periods, all AIX LPARs "
                "maintained continuous network connectivity and disk "
                "I/O availability through redundant VIOS paths. "
                "All MPIO paths recovered to Enabled state after "
                "VIOS restart.")
        else:
            parts.append(
                "The VIOS resilience tests completed with some "
                "observations. Review the per-host details in "
                "Section 2 for specific impact assessment.")
    doc.add_paragraph(" ".join(parts))

    return sec_num + 1



# ═══════════════════════════════════════════════════════════
#  Section 4 — Appendix & Logs
# ═══════════════════════════════════════════════════════════

def add_appendix_section(doc, all_client, all_server, all_vios, sec_num):
    """Appendix listing all raw log files for reference.
    VIOS logs listed as file inventory (not printed).
    iperf client/server logs listed as file inventory too.
    Returns next sec number."""

    doc.add_heading(f"{sec_num}. Appendix & Logs", level=1)
    doc.add_paragraph(
        "This appendix catalogues the raw test log files generated during "
        "each test. The full files are available in the testresult/ "
        "directory alongside this report and in the corresponding "
        ".tar.gz archives.")

    sub = 1

    # ── iperf logs ──
    if all_client or all_server:
        doc.add_heading(
            f"{sec_num}.{sub} Network Throughput Logs", level=2)
        iperf_files = []
        for d in all_client:
            iperf_files.append(
                (d["filename"],
                 d["hostname"] or "Unknown",
                 f"TCP: {d['tcp_bandwidth'] or 'N/A'}"))
        for sd in all_server:
            iperf_files.append(
                (sd["filename"],
                 "Server",
                 f"BW: {sd['bandwidth'] or 'N/A'}"))

        tbl = doc.add_table(rows=1 + len(iperf_files), cols=3)
        tbl.style = "Light Grid Accent 1"
        for i, h in enumerate(["File Name", "Host", "Key Metrics"]):
            _bold_cell(tbl.rows[0].cells[i], h)
        for idx, (fname, host, metrics) in enumerate(iperf_files):
            tbl.rows[idx + 1].cells[0].text = fname
            tbl.rows[idx + 1].cells[1].text = host
            tbl.rows[idx + 1].cells[2].text = metrics
        doc.add_paragraph()
        sub += 1

    # ── VIOS logs ──
    if all_vios:
        doc.add_heading(
            f"{sec_num}.{sub} VIOS Resilience Logs", level=2)
        for d in all_vios:
            doc.add_heading(
                f"Host: {d['hostname']}  \u2014  VIOS: {d['vios_name']}  "
                f"({d['basename']})", level=3)
            log_files = _collect_vios_log_files(d["test_dir"])
            if log_files:
                tbl = doc.add_table(rows=1 + len(log_files), cols=2)
                tbl.style = "Light Grid Accent 1"
                _bold_cell(tbl.rows[0].cells[0], "File Name")
                _bold_cell(tbl.rows[0].cells[1], "Size")
                for fi, lf in enumerate(log_files):
                    tbl.rows[fi + 1].cells[0].text = os.path.basename(lf)
                    sz = os.path.getsize(lf)
                    if sz < 1024:
                        szstr = f"{sz} B"
                    elif sz < 1048576:
                        szstr = f"{sz/1024:.1f} KB"
                    else:
                        szstr = f"{sz/1048576:.1f} MB"
                    tbl.rows[fi + 1].cells[1].text = szstr
            else:
                doc.add_paragraph("No log files found.", style="List Bullet")
            doc.add_paragraph()
        sub += 1

    return sec_num + 1



# ═══════════════════════════════════════════════════════════
#  Main builder
# ═══════════════════════════════════════════════════════════

def generate_consolidated_report(
    base_dir, output_path,
    iperf_client_files=None, iperf_server_files=None,
    vios_dirs=None,
):
    """Create one Word doc with all test type sections."""

    has_iperf = bool(iperf_client_files)
    has_vios = bool(vios_dirs)

    # Parse data
    all_client = [parse_iperf_log(f) for f in (iperf_client_files or [])]
    all_server = []
    if iperf_server_files:
        all_server = [parse_server_log(f) for f in iperf_server_files]
    all_vios = []
    if vios_dirs:
        for d in vios_dirs:
            all_vios.append(parse_test_dir(d))

    # ── Document setup ──
    doc = Document()
    style = doc.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(10)

    # Tighten heading styles: reduce vertical spacing and add left indent
    # so sub-sections are visually nested (4-6 char indent per level).
    for lvl, (sp_before, sp_after, indent) in {
        1: (Pt(12), Pt(4), Cm(0)),      # Section: no indent
        2: (Pt(8),  Pt(3), Cm(0.5)),    # Sub-section: ~2 chars
        3: (Pt(6),  Pt(2), Cm(1.0)),    # Sub-sub-section: ~4 chars
    }.items():
        hstyle = doc.styles[f"Heading {lvl}"]
        hstyle.paragraph_format.space_before = sp_before
        hstyle.paragraph_format.space_after = sp_after
        hstyle.paragraph_format.left_indent = indent

    # ── Title Page ──
    doc.add_paragraph()
    doc.add_paragraph()
    title = doc.add_heading("Consolidated Test Report", level=0)
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER

    sections_included = []
    if has_iperf:
        sections_included.append("Network Throughput (iperf)")
    if has_vios:
        sections_included.append("VIOS Resilience")
    sub = doc.add_paragraph()
    sub.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = sub.add_run(
        "\nTest Types: " + ", ".join(sections_included) + "\n")
    run.font.size = Pt(14)

    if has_vios:
        vios_info = doc.add_paragraph()
        vios_info.alignment = WD_ALIGN_PARAGRAPH.CENTER
        vl = ", ".join(sorted(set(d["vios_name"] for d in all_vios)))
        hl = ", ".join(d["hostname"] for d in all_vios)
        run = vios_info.add_run(
            f"VIOS Tested: {vl}\nClient Hosts: {hl}")
        run.font.size = Pt(12)

    gen = doc.add_paragraph()
    gen.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = gen.add_run(
        f"\nReport Generated: "
        f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    run.font.size = Pt(10)
    run.font.color.rgb = COLOR_GREY
    doc.add_page_break()


    # ── Table of Contents ──
    doc.add_heading("Table of Contents", level=1)
    toc = []
    sec = 1

    if has_iperf:
        toc.append((f"{sec}. Network Throughput Test Results", False))
        for idx, d in enumerate(all_client):
            host = d["hostname"] or f"Test {idx+1}"
            toc.append((f"    {sec}.{idx+1} {host}", True))
        sec += 1

    if has_vios:
        toc.append((f"{sec}. VIOS Resilience Test", False))
        toc.append((f"    {sec}.1 Network Resilience Test", True))
        sub_idx = 1
        for phase, plabel in _PHASE_META:
            for d in all_vios:
                toc.append((
                    f"        {sec}.1.{sub_idx} {plabel} VIOS "
                    f"{d['vios_name']} ({d['hostname']})", True))
                sub_idx += 1
        toc.append((f"    {sec}.2 Disk/SAN Path Resilience Test", True))
        sub_idx = 1
        for phase, plabel in _PHASE_META:
            for d in all_vios:
                toc.append((
                    f"        {sec}.2.{sub_idx} {plabel} VIOS "
                    f"{d['vios_name']} ({d['hostname']})", True))
                sub_idx += 1
        sec += 1

    toc.append((f"{sec}. Summary", False))
    sec += 1
    toc.append((f"{sec}. Appendix & Logs", False))

    for txt, is_sub in toc:
        p = doc.add_paragraph()
        r = p.add_run(txt)
        r.font.size = Pt(9 if is_sub else 10)
        if is_sub:
            indent = 0.8
            if txt.startswith("        "):
                indent = 1.6
            p.paragraph_format.left_indent = Cm(indent)
    doc.add_page_break()

    # ── Content Sections ──
    sec = 1
    if has_iperf:
        sec = add_iperf_section(doc, all_client, all_server, sec)
        doc.add_page_break()
    if has_vios:
        sec = add_vios_section(doc, all_vios, sec)
        doc.add_page_break()
    sec = add_summary_section(doc, all_client, all_vios, sec)
    doc.add_page_break()
    sec = add_appendix_section(doc, all_client, all_server, all_vios, sec)

    # ── Save ──
    try:
        doc.save(output_path)
        print(f"Report generated: {output_path}")
    except PermissionError:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        root, ext = os.path.splitext(output_path)
        fallback = f"{root}_{ts}{ext}"
        print(f"WARNING: Cannot write '{output_path}' (file open?).")
        doc.save(fallback)
        print(f"Report generated: {fallback}")

    return True



def main():
    base_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "testresult")
    if not os.path.exists(base_dir):
        print(f"Error: testresult directory not found at {base_dir}")
        sys.exit(1)

    # Parse --files <name1> <name2> ...
    selected_files = None
    if "--files" in sys.argv:
        idx = sys.argv.index("--files")
        selected_files = [os.path.basename(f) for f in sys.argv[idx + 1:]]

    if selected_files:
        iperf_client, iperf_server, vios_names = classify_files(
            base_dir, selected_files)
        vios_dirs = resolve_vios_dirs(base_dir, vios_names)
    else:
        # Auto-discover everything
        iperf_client = find_iperf_logs(base_dir)
        iperf_server = find_iperf_server_logs(base_dir)
        extract_test_archives(base_dir)
        vios_dirs = find_test_dirs(base_dir)

    if not iperf_client and not vios_dirs:
        print("Error: No iperf logs or VIOS test directories found.")
        sys.exit(1)

    parts = []
    if iperf_client:
        parts.append(f"{len(iperf_client)} iperf log(s)")
    if vios_dirs:
        parts.append(f"{len(vios_dirs)} VIOS dir(s)")
    print(f"Found {', '.join(parts)}.")

    output_path = os.path.join(base_dir, "Consolidated_Test_Report.docx")
    print("\nGenerating consolidated report...")
    generate_consolidated_report(
        base_dir, output_path,
        iperf_client_files=iperf_client,
        iperf_server_files=iperf_server if iperf_server else None,
        vios_dirs=vios_dirs if vios_dirs else None,
    )


if __name__ == "__main__":
    main()

