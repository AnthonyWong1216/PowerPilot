// PowerPilot Test Center
// Handles: iperf, VIOS resilience, CPU/mem, I/O tests + results/reports
(function () {
  "use strict";

  // ── Checkbox <-> panel toggling ─────────────────────────
  var panels = {
    "chk-iperf": "panel-iperf",
    "chk-vios-net": "panel-vios-net",
    "chk-vios-fc": "panel-vios-fc",
    "chk-cpu-mem": "panel-cpu-mem",
    "chk-io": "panel-io"
  };
  var labels = {
    "chk-iperf": "Network Throughput (iperf)",
    "chk-vios-net": "VIOS Network Resilience",
    "chk-vios-fc": "VIOS Fibre Channel Resilience",
    "chk-cpu-mem": "CPU & Memory Benchmark",
    "chk-io": "I/O Performance"
  };
  var actionCard = document.getElementById("action-card");
  var summaryEl  = document.getElementById("action-summary");

  function getSelected() {
    var sel = [];
    for (var id in panels) { if (document.getElementById(id).checked) sel.push(id); }
    return sel;
  }

  function updatePanels() {
    var selected = [];
    for (var chkId in panels) {
      var chk = document.getElementById(chkId);
      var panel = document.getElementById(panels[chkId]);
      if (chk.checked) { panel.style.display = ""; selected.push(labels[chkId]); }
      else { panel.style.display = "none"; }
    }
    if (selected.length) { actionCard.style.display = ""; summaryEl.textContent = "Selected: " + selected.join(", "); }
    else { actionCard.style.display = "none"; }
  }
  for (var chkId in panels) { document.getElementById(chkId).addEventListener("change", updatePanels); }

  // ── Utilities ───────────────────────────────────────────
  function esc(t) {
    return String(t).replace(/[&<>]/g, function(c){ return {"&":"&amp;","<":"&lt;",">":"&gt;"}[c]; });
  }
  function humanSize(b) {
    if (b < 1024) return b + " B";
    if (b < 1048576) return (b/1024).toFixed(1) + " KB";
    return (b/1048576).toFixed(1) + " MB";
  }
  function renderSteps(container, steps) {
    (steps||[]).forEach(function(s) {
      var d = document.createElement("div");
      d.className = "step-row " + (s.ok ? "step-ok" : "step-fail");
      var h = "<strong>" + (s.ok?"\u2713":"\u2717") + " Step " + s.step + ": " + esc(s.label) + "</strong>";
      var body = (s.error && !s.ok) ? s.error : s.output;
      if (body) h += '<div class="step-out">' + esc(body) + "</div>";
      d.innerHTML = h; container.appendChild(d);
    });
  }

  // ── DOM refs ────────────────────────────────────────────
  var runBtn       = document.getElementById("btn-run-tests");
  var runStatus    = document.getElementById("run-status");
  var runSteps     = document.getElementById("run-steps");
  var manualBox    = document.getElementById("run-manual");
  var manualCmd    = document.getElementById("run-manual-cmd");
  var fetchSection = document.getElementById("fetch-section");
  var fetchBtn     = document.getElementById("btn-fetch");
  var fetchStatus  = document.getElementById("fetch-status");
  var fetchSteps   = document.getElementById("fetch-steps");
  var fetchBtnInline  = document.getElementById("btn-fetch-inline");
  var fetchStatusInline = document.getElementById("fetch-status-inline");
  var fetchStepsInline  = document.getElementById("fetch-steps-inline");
  var archivesEl   = document.getElementById("archives-list");
  var reportsEl    = document.getElementById("reports-list");
  var reportStat   = document.getElementById("report-status");
  var consolidatedBar = document.getElementById("consolidated-bar");
  var consolidatedBtn = document.getElementById("btn-consolidated");

  var lastViosConn = null;  // for fetch step
  var allGroupCheckboxes = {};  // groupId -> [checkbox, ...]

  // ── Ping target IP management ──────────────────────────────
  var vnetTargetsList = document.getElementById("vnet-targets-list");
  var vnetAddBtn      = document.getElementById("vnet-add-target");
  var _pingTargetIdx  = 0;

  function addPingTargetRow(prefill) {
    var idx = _pingTargetIdx++;
    var ifName = "en" + idx;
    var row = document.createElement("div");
    row.className = "ping-target-row";
    row.setAttribute("data-idx", idx);
    row.innerHTML =
      '<span class="ping-iface">' + ifName + ' =</span>' +
      '<input type="text" class="fld vnet-target-ip" placeholder="e.g. 10.1.1.1" value="' + (prefill || "") + '">' +
      '<button type="button" class="ping-target-del" title="Remove">&times;</button>';
    row.querySelector(".ping-target-del").addEventListener("click", function() {
      row.remove();
      renumberPingTargets();
    });
    vnetTargetsList.appendChild(row);
  }

  function renumberPingTargets() {
    var rows = vnetTargetsList.querySelectorAll(".ping-target-row");
    for (var i = 0; i < rows.length; i++) {
      rows[i].querySelector(".ping-iface").textContent = "en" + i + " =";
      rows[i].setAttribute("data-idx", i);
    }
    _pingTargetIdx = rows.length;
  }

  function getVnetPingTargets() {
    var targets = [];
    var inputs = vnetTargetsList.querySelectorAll(".vnet-target-ip");
    for (var i = 0; i < inputs.length; i++) {
      var ip = inputs[i].value.trim();
      if (ip) targets.push(ip);
    }
    return targets;
  }

  vnetAddBtn.addEventListener("click", function() {
    addPingTargetRow("");
  });

  // ── Test runners (return Promises) ───────────────────────
  function runTest(chkId) {
    if (chkId === "chk-iperf") return runIperf();
    if (chkId === "chk-vios-net") return runViosDeploy("vnet");
    if (chkId === "chk-vios-fc") return runViosDeploy("vfc");
    if (chkId === "chk-cpu-mem" || chkId === "chk-io") {
      var info = document.createElement("div");
      info.className = "step-row step-info";
      info.innerHTML = "<strong>\u2139 " + labels[chkId] + "</strong> &mdash; Coming soon. Deploy scripts manually via the Deploy Scripts page.";
      runSteps.appendChild(info);
      return Promise.resolve();
    }
    return Promise.resolve();
  }

  // ── iperf test ──────────────────────────────────────────
  function runIperf() {
    var payload = {
      server: {
        host: document.getElementById("iperf-srv-host").value.trim(),
        username: document.getElementById("iperf-srv-user").value.trim(),
        password: document.getElementById("iperf-srv-pass").value,
        port: document.getElementById("iperf-srv-port").value || 22
      },
      client: {
        host: document.getElementById("iperf-cli-host").value.trim(),
        username: document.getElementById("iperf-cli-user").value.trim(),
        password: document.getElementById("iperf-cli-pass").value,
        port: document.getElementById("iperf-cli-port").value || 22
      },
      iperf_port: document.getElementById("iperf-port").value || 5201,
      duration: document.getElementById("iperf-duration").value || 10,
      udp_bandwidth: document.getElementById("iperf-udp-bw").value.trim() || "1G"
    };
    if (!payload.server.host || !payload.client.host) {
      runStatus.textContent = "\u2717 Server and Client host are required for iperf.";
      return Promise.resolve();
    }
    var hdr = document.createElement("div");
    hdr.className = "step-row step-info";
    hdr.innerHTML = "<strong>\u25b6 Running Network Throughput (iperf)\u2026</strong>";
    runSteps.appendChild(hdr);

    return fetch("/api/test/iperf", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    })
    .then(function(r){ return r.json(); })
    .then(function(res){
      hdr.className = "step-row " + (res.ok ? "step-ok" : "step-fail");
      hdr.innerHTML = "<strong>" + (res.ok?"\u2713":"\u2717") + " Network Throughput (iperf)</strong>";
      renderSteps(runSteps, res.steps);
      if (res.ok) loadResults();
    });
  }

  // ── VIOS resilience deploy ──────────────────────────────
  function runViosDeploy(prefix) {
    var host = document.getElementById(prefix + "-host").value.trim();
    var user = document.getElementById(prefix + "-user").value.trim();
    var pass = document.getElementById(prefix + "-pass").value;
    var port = document.getElementById(prefix + "-port").value || 22;
    var label = document.getElementById(prefix + "-label").value.trim();
    var mode = document.getElementById(prefix + "-mode").value;
    var secs = document.getElementById(prefix + "-secs").value || 180;
    var tpath = document.getElementById(prefix + "-path").value.trim() || "/tmp";
    if (!host || !label) {
      runStatus.textContent = "\u2717 Host and label are required for VIOS resilience.";
      return Promise.resolve();
    }
    lastViosConn = { host: host, username: user, password: pass, port: port };
    // Auto-populate the standalone fetch card fields
    document.getElementById("fetch-host").value = host;
    document.getElementById("fetch-user").value = user;
    document.getElementById("fetch-pass").value = pass;
    document.getElementById("fetch-port").value = port;
    var payload = { host:host, username:user, password:pass, port:port,
                    label:label, mode:mode, seconds:secs, target_path:tpath };
    // Include ping target IPs for network resilience tests
    if (prefix === "vnet") {
      var targets = getVnetPingTargets();
      if (targets.length) payload.ping_targets = targets;
    }
    var lbl = (prefix === "vnet") ? "VIOS Network Resilience" : "VIOS Fibre Channel Resilience";
    var hdr = document.createElement("div");
    hdr.className = "step-row step-info";
    hdr.innerHTML = "<strong>\u25b6 Deploying " + lbl + "\u2026</strong>";
    runSteps.appendChild(hdr);

    return fetch("/api/test/deploy", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    })
    .then(function(r){ return r.json(); })
    .then(function(res){
      hdr.className = "step-row " + (res.ok ? "step-ok" : "step-fail");
      hdr.innerHTML = "<strong>" + (res.ok?"\u2713":"\u2717") + " " + lbl + " Deploy</strong>";
      renderSteps(runSteps, res.steps);
      if (res.ok && res.manual_command) {
        manualCmd.textContent = res.manual_command;
        manualBox.style.display = "block";
        fetchSection.style.display = "block";
      }
    });
  }

  // ── Copy manual command ───────────────────────────────────
  document.getElementById("btn-copy-cmd").addEventListener("click", function(){
    var text = manualCmd.textContent || "";
    if (!text) return;
    var btn = this;
    navigator.clipboard.writeText(text).then(function(){
      btn.textContent = "Copied!";
      setTimeout(function(){ btn.textContent = "Copy"; }, 1500);
    });
  });

  // ── Fetch results (shared helper) ─────────────────────────
  function doFetch(conn, btn, statusEl, stepsEl) {
    if (!conn || !conn.host) {
      statusEl.textContent = "\u2717 Target host is required.";
      return;
    }
    btn.disabled = true;
    btn.textContent = "Fetching\u2026";
    statusEl.textContent = "Looking for the newest result archive\u2026";
    stepsEl.innerHTML = "";
    fetch("/api/test/fetch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(conn)
    })
    .then(function(r){ return r.json(); })
    .then(function(res){
      renderSteps(stepsEl, res.steps);
      statusEl.textContent = res.ok
        ? ("\u2713 " + (res.message || "Fetched."))
        : ("\u2717 " + (res.error || "Fetch failed."));
      if (res.ok) loadResults();
    })
    .catch(function(e){ statusEl.textContent = "\u2717 " + e; })
    .then(function(){ btn.disabled = false; btn.textContent = "Fetch Results"; });
  }

  // ── Standalone fetch card (always visible, has its own fields) ──
  fetchBtn.addEventListener("click", function(){
    var conn = {
      host:     document.getElementById("fetch-host").value.trim(),
      username: document.getElementById("fetch-user").value.trim() || "root",
      password: document.getElementById("fetch-pass").value,
      port:     parseInt(document.getElementById("fetch-port").value) || 22
    };
    doFetch(conn, fetchBtn, fetchStatus, fetchSteps);
  });

  // ── Inline fetch (inside Step 2, uses lastViosConn from deploy) ──
  if (fetchBtnInline) {
    fetchBtnInline.addEventListener("click", function(){
      if (!lastViosConn || !lastViosConn.host) {
        fetchStatusInline.textContent = "\u2717 No target host \u2014 run a VIOS deploy first.";
        return;
      }
      doFetch(lastViosConn, fetchBtnInline, fetchStatusInline, fetchStepsInline);
    });
  }

  // ── Run button ──────────────────────────────────────────
  runBtn.addEventListener("click", function(){
    var sel = getSelected();
    if (!sel.length) return;
    runBtn.disabled = true;
    runBtn.textContent = "Running\u2026";
    runStatus.textContent = "";
    runSteps.innerHTML = "";
    manualBox.style.display = "none";
    fetchSection.style.display = "none";

    var chain = Promise.resolve();
    sel.forEach(function(chkId){
      chain = chain.then(function(){ return runTest(chkId); });
    });
    chain.then(function(){
      runStatus.textContent = "\u2713 All selected tests processed.";
      loadResults();
    }).catch(function(err){
      runStatus.textContent = "\u2717 Error: " + err;
    }).then(function(){
      runBtn.disabled = false;
      runBtn.textContent = "Run Selected Tests";
    });
  });

  // ── File grouping & classification ─────────────────────
  var FILE_GROUPS = [
    { id: "throughput", icon: "\uD83C\uDF10", title: "Network Throughput (iperf)",
      match: function(n){ return n.indexOf("iperf") >= 0; },
      api: "/api/test/iperf-report", genLabel: "Generate Throughput Report" },
    { id: "vios", icon: "\uD83D\uDD04", title: "VIOS Resilience",
      match: function(n){ return n.indexOf("vios_res_") >= 0; },
      api: "/api/test/report", genLabel: "Generate VIOS Report" },
    { id: "cpu", icon: "\u2699\uFE0F", title: "CPU & Memory Benchmark",
      match: function(n){ return n.indexOf("cpu_mem") >= 0 || n.indexOf("cpu_memory") >= 0; },
      api: null, genLabel: "Generate Report" },
    { id: "io", icon: "\uD83D\uDCC0", title: "I/O Performance",
      match: function(n){ return n.indexOf("io_perf") >= 0 || n.indexOf("io_performance") >= 0; },
      api: null, genLabel: "Generate Report" },
    { id: "other", icon: "\uD83D\uDCC1", title: "Other Files",
      match: function(){ return true; }, api: null, genLabel: "" }
  ];

  function classifyFile(name) {
    for (var i = 0; i < FILE_GROUPS.length; i++) {
      if (FILE_GROUPS[i].id !== "other" && FILE_GROUPS[i].match(name)) return FILE_GROUPS[i].id;
    }
    return "other";
  }

  // ── Results list ────────────────────────────────────────
  function loadResults() {
    fetch("/api/test/results")
      .then(function(r){ return r.json(); })
      .then(function(res){
        if (!res.ok) { archivesEl.textContent = "Failed to load."; return; }
        if (!res.archives.length) {
          archivesEl.innerHTML = '<div class="rpt-empty">No result files yet. Run a test above.</div>';
        } else {
          renderGroupedArchives(res.archives);
        }
        if (!res.reports.length) {
          reportsEl.textContent = "No report generated yet.";
        } else {
          reportsEl.innerHTML = "";
          res.reports.forEach(function(f){ reportsEl.appendChild(reportRow(f)); });
        }
      })
      .catch(function(e){ archivesEl.textContent = "Error: " + e; });
  }

  function renderGroupedArchives(archives) {
    var buckets = {};
    FILE_GROUPS.forEach(function(g){ buckets[g.id] = []; });
    archives.forEach(function(f){ buckets[classifyFile(f.name)].push(f); });
    archivesEl.innerHTML = "";
    allGroupCheckboxes = {};
    FILE_GROUPS.forEach(function(g){
      if (buckets[g.id].length) archivesEl.appendChild(buildGroup(g, buckets[g.id]));
    });
    updateConsolidatedBar();
  }

  function updateConsolidatedBar() {
    // Show bar when at least 1 group has checkboxes (i.e. there are files)
    var groupCount = 0;
    for (var gid in allGroupCheckboxes) {
      if (allGroupCheckboxes[gid].length) groupCount++;
    }
    consolidatedBar.style.display = groupCount > 0 ? "" : "none";
  }

  function getAllCheckedFiles() {
    var files = [];
    for (var gid in allGroupCheckboxes) {
      allGroupCheckboxes[gid].forEach(function(c){
        if (c.checked) files.push(c.getAttribute("data-filename"));
      });
    }
    return files;
  }

  function buildGroup(group, files) {
    var wrap = document.createElement("div");
    wrap.className = "rpt-group";
    // Header
    var hdr = document.createElement("div");
    hdr.className = "rpt-group-hdr";
    var titleEl = document.createElement("span");
    titleEl.className = "rpt-group-title";
    titleEl.innerHTML = group.icon + " " + esc(group.title)
      + ' <span class="rpt-count">(' + files.length + " file" + (files.length>1?"s":"") + ")</span>";
    hdr.appendChild(titleEl);
    var actions = document.createElement("span");
    actions.className = "rpt-group-actions";
    var selBtn = document.createElement("button");
    selBtn.className = "btn-plain"; selBtn.style.fontSize = "11px";
    selBtn.textContent = "Deselect All"; selBtn.setAttribute("data-state", "all");
    actions.appendChild(selBtn);
    var genBtn = null;
    if (group.api && group.genLabel) {
      genBtn = document.createElement("button");
      genBtn.className = "rpt-gen-btn";
      genBtn.textContent = group.genLabel;
      actions.appendChild(genBtn);
    }
    hdr.appendChild(actions);
    wrap.appendChild(hdr);
    // Body — file rows
    var body = document.createElement("div");
    body.className = "rpt-group-body";
    var checkboxes = [];
    files.forEach(function(f){
      var row = document.createElement("div");
      row.className = "rpt-file-row";
      var chk = document.createElement("input");
      chk.type = "checkbox"; chk.className = "rpt-file-chk";
      chk.checked = true; chk.title = "Include in report";
      chk.setAttribute("data-filename", f.name);
      checkboxes.push(chk); row.appendChild(chk);
      var nameSpan = document.createElement("span");
      nameSpan.className = "rpt-file-name"; nameSpan.textContent = f.name; nameSpan.title = f.name;
      row.appendChild(nameSpan);
      var sizeSpan = document.createElement("span");
      sizeSpan.className = "rpt-file-size"; sizeSpan.textContent = humanSize(f.size);
      row.appendChild(sizeSpan);
      var fa = document.createElement("span");
      fa.className = "rpt-file-actions";
      fa.appendChild(downloadLink(f.name)); fa.appendChild(makeDelBtn(f.name));
      row.appendChild(fa);
      body.appendChild(row);
    });
    wrap.appendChild(body);
    // Register checkboxes in global map for consolidated report
    allGroupCheckboxes[group.id] = checkboxes;
    // Toggle logic
    selBtn.addEventListener("click", function(){
      var on = selBtn.getAttribute("data-state") === "all";
      checkboxes.forEach(function(c){ c.checked = !on; });
      selBtn.textContent = on ? "Select All" : "Deselect All";
      selBtn.setAttribute("data-state", on ? "none" : "all");
    });
    // Generate report
    if (genBtn && group.api) {
      genBtn.addEventListener("click", function(){
        var sel = [];
        checkboxes.forEach(function(c){ if (c.checked) sel.push(c.getAttribute("data-filename")); });
        if (!sel.length) { reportStat.textContent = "\u26A0 No files selected."; return; }
        genBtn.disabled = true; var orig = genBtn.textContent;
        genBtn.textContent = "Generating\u2026";
        reportStat.textContent = "Generating report for " + sel.length + " file(s)\u2026";
        fetch(group.api, { method:"POST", headers:{"Content-Type":"application/json"},
          body: JSON.stringify({ files: sel }) })
          .then(function(r){ return r.json(); })
          .then(function(res){
            reportStat.textContent = res.ok ? ("\u2713 "+(res.message||"Done.")) : ("\u2717 "+(res.error||"Failed."));
            loadResults();
          })
          .catch(function(e){ reportStat.textContent = "\u2717 " + e; })
          .then(function(){ genBtn.disabled = false; genBtn.textContent = orig; });
      });
    }
    return wrap;
  }

  function downloadLink(name) {
    var a = document.createElement("a");
    a.href = "/api/test/download/" + encodeURIComponent(name);
    a.textContent = "Download"; a.className = "btn-plain";
    a.style.cssText = "font-size:12px;padding:3px 8px;text-decoration:none;";
    return a;
  }
  function makeDelBtn(name) {
    var btn = document.createElement("button");
    btn.className = "btn-plain"; btn.style.cssText = "font-size:12px;color:var(--red);";
    btn.textContent = "Delete";
    btn.addEventListener("click", function(){
      if (!confirm("Delete " + name + "?")) return;
      btn.disabled = true;
      fetch("/api/test/delete/" + encodeURIComponent(name), { method:"DELETE" })
        .then(function(r){ return r.json(); }).then(function(){ loadResults(); })
        .catch(function(e){ alert("Delete failed: "+e); btn.disabled=false; });
    });
    return btn;
  }
  function reportRow(f) {
    var row = document.createElement("div"); row.className = "res-row";
    var left = document.createElement("span");
    left.style.fontFamily = "monospace";
    left.textContent = f.name + "  (" + humanSize(f.size) + ")";
    var right = document.createElement("span"); right.style.cssText = "display:flex;gap:6px;";
    right.appendChild(downloadLink(f.name)); right.appendChild(makeDelBtn(f.name));
    row.appendChild(left); row.appendChild(right);
    return row;
  }

  // ── Consolidated Report button ───────────────────────────
  consolidatedBtn.addEventListener("click", function(){
    var sel = getAllCheckedFiles();
    if (!sel.length) { reportStat.textContent = "\u26A0 No files selected across any group."; return; }
    consolidatedBtn.disabled = true;
    var orig = consolidatedBtn.textContent;
    consolidatedBtn.textContent = "Generating\u2026";
    reportStat.textContent = "Generating consolidated report for " + sel.length + " file(s)\u2026";
    fetch("/api/test/consolidated-report", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ files: sel })
    })
    .then(function(r){ return r.json(); })
    .then(function(res){
      reportStat.textContent = res.ok
        ? ("\u2713 " + (res.message || "Done."))
        : ("\u2717 " + (res.error || "Failed."));
      loadResults();
    })
    .catch(function(e){ reportStat.textContent = "\u2717 " + e; })
    .then(function(){ consolidatedBtn.disabled = false; consolidatedBtn.textContent = orig; });
  });

  // ── Refresh + Init ──────────────────────────────────────
  document.getElementById("btn-refresh").addEventListener("click", loadResults);
  loadResults();
})();
