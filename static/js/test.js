// VIOS Resilience Test page logic:
//  - Step 1: Deploy vios_res_client.sh to a target LPAR (SFTP)
//  - Step 2: Show the exact command to run manually (SSH/console) — the
//            actual VIOS shutdown/restart must be triggered by the operator
//            on the HMC at the right moment, so this step is NOT automated.
//  - Step 3: Fetch Results — locate + download the newest tar.gz once the
//            manual run has completed on the LPAR.
//  - List downloaded result archives and generated reports.
//  - Generate the consolidated Word report from testresult/.
(function () {
  const form         = document.getElementById("test-form");
  const runBtn       = document.getElementById("t-run");
  const statusEl     = document.getElementById("t-status");
  const stepsEl      = document.getElementById("t-steps");
  const manualBox    = document.getElementById("t-manual");
  const manualCmdEl  = document.getElementById("t-manual-cmd");
  const copyCmdBtn   = document.getElementById("t-copy-cmd");
  const fetchSection = document.getElementById("t-fetch-section");
  const fetchBtn     = document.getElementById("t-fetch");
  const fetchStatus  = document.getElementById("t-fetch-status");
  const fetchStepsEl = document.getElementById("t-fetch-steps");
  const refreshBtn   = document.getElementById("t-refresh");
  const reportStat   = document.getElementById("report-status");

  const archivesEl   = document.getElementById("archives-list");
  const reportsEl    = document.getElementById("reports-list");

  function humanSize(b) {
    if (b < 1024) return b + " B";
    if (b < 1048576) return (b / 1024).toFixed(1) + " KB";
    return (b / 1048576).toFixed(1) + " MB";
  }

  function renderSteps(container, steps) {
    container.innerHTML = "";
    (steps || []).forEach(s => {
      const div = document.createElement("div");
      div.className = "step-row " + (s.ok ? "step-ok" : "step-fail");
      let html = "<strong>" + (s.ok ? "✓" : "✗") + " Step " + s.step +
                 ": " + s.label + "</strong>";
      const body = (s.error && !s.ok) ? s.error : s.output;
      if (body) html += '<div class="step-out">' + escapeHtml(body) + "</div>";
      div.innerHTML = html;
      container.appendChild(div);
    });
  }

  function escapeHtml(t) {
    return String(t).replace(/[&<>]/g, c =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
  }

  function loadResults() {
    fetch("/api/test/results")
      .then(r => r.json())
      .then(res => {
        if (!res.ok) { archivesEl.textContent = "Failed to load results."; return; }
        // Archives (tarball results) — each row gets a "Generate Report" button
        if (!res.archives.length) {
          archivesEl.textContent = "No result archives yet. Run a test above.";
        } else {
          archivesEl.innerHTML = "";
          res.archives.forEach(f => archivesEl.appendChild(archiveRow(f)));
        }
        // Reports — each row gets a "Delete" button
        if (!res.reports.length) {
          reportsEl.textContent = "No report generated yet.";
        } else {
          reportsEl.innerHTML = "";
          res.reports.forEach(f => reportsEl.appendChild(reportRow(f)));
        }
      })
      .catch(e => { archivesEl.textContent = "Error: " + e; });
  }

  function baseRow(f) {
    const row = document.createElement("div");
    row.className = "res-row";
    const left = document.createElement("span");
    left.style.fontFamily = "monospace";
    left.textContent = f.name + "  (" + humanSize(f.size) + ")";
    row.appendChild(left);
    const actions = document.createElement("span");
    actions.style.display = "flex";
    actions.style.gap = "6px";
    row.appendChild(actions);
    return { row, actions };
  }

  function downloadLink(name) {
    const a = document.createElement("a");
    a.href = "/api/test/download/" + encodeURIComponent(name);
    a.textContent = "Download";
    a.className = "btn-plain";
    a.style.textDecoration = "none";
    return a;
  }

  function archiveRow(f) {
    const { row, actions } = baseRow(f);
    actions.appendChild(downloadLink(f.name));

    const genBtn = document.createElement("button");
    genBtn.type = "button";
    genBtn.className = "btn-accent";
    genBtn.textContent = "Generate Report";
    genBtn.addEventListener("click", () => {
      genBtn.disabled = true;
      genBtn.textContent = "Generating…";
      reportStat.textContent = "Generating consolidated Word report from testresult/…";
      fetch("/api/test/report", { method: "POST" })
        .then(r => r.json())
        .then(res => {
          if (res.ok) {
            reportStat.textContent = "✓ Report generated: " + res.report;
            loadResults();
          } else {
            reportStat.textContent = "✗ " + (res.error || "Report generation failed.");
          }
        })
        .catch(e => { reportStat.textContent = "✗ " + e; })
        .finally(() => {
          genBtn.disabled = false;
          genBtn.textContent = "Generate Report";
        });
    });
    actions.appendChild(genBtn);
    actions.appendChild(makeDeleteBtn(f.name, "archive"));
    return row;
  }

  function makeDeleteBtn(name, kindLabel) {
    const delBtn = document.createElement("button");
    delBtn.type = "button";
    delBtn.className = "btn-plain";
    delBtn.textContent = "Delete";
    delBtn.addEventListener("click", () => {
      if (!confirm("Delete " + kindLabel + " \"" + name + "\"?")) return;
      delBtn.disabled = true;
      delBtn.textContent = "Deleting…";
      fetch("/api/test/delete/" + encodeURIComponent(name), { method: "DELETE" })
        .then(r => r.json())
        .then(res => {
          if (res.ok) {
            reportStat.textContent = "✓ " + (res.message || "Deleted.");
            loadResults();
          } else {
            reportStat.textContent = "✗ " + (res.error || "Delete failed.");
            delBtn.disabled = false;
            delBtn.textContent = "Delete";
          }
        })
        .catch(e => {
          reportStat.textContent = "✗ " + e;
          delBtn.disabled = false;
          delBtn.textContent = "Delete";
        });
    });
    return delBtn;
  }

  function reportRow(f) {
    const { row, actions } = baseRow(f);
    actions.appendChild(downloadLink(f.name));
    actions.appendChild(makeDeleteBtn(f.name, "report"));
    return row;
  }



  // Keep the last-used connection details around so "Fetch Results" can
  // reuse them without asking the user to re-enter host/user/password.
  let lastConn = null;

  form.addEventListener("submit", e => {
    e.preventDefault();
    const payload = {
      host:        document.getElementById("t-host").value.trim(),
      username:    document.getElementById("t-user").value.trim(),
      password:    document.getElementById("t-pass").value,
      port:        document.getElementById("t-port").value || 22,
      label:       document.getElementById("t-label").value.trim(),
      mode:        document.getElementById("t-mode").value,
      seconds:     document.getElementById("t-secs").value || 180,
      target_path: document.getElementById("t-path").value.trim() || "/tmp",
    };
    lastConn = {
      host: payload.host, username: payload.username,
      password: payload.password, port: payload.port,
    };
    runBtn.disabled = true;
    runBtn.textContent = "Deploying…";
    statusEl.textContent = "Deploying script via SFTP…";
    stepsEl.innerHTML = "";
    manualBox.style.display = "none";
    fetchSection.style.display = "none";
    fetch("/api/test/deploy", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    })
      .then(r => r.json())
      .then(res => {
        renderSteps(stepsEl, res.steps);
        if (res.ok) {
          statusEl.textContent = "✓ " + (res.message || "Deployed.");
          manualCmdEl.textContent = res.manual_command || "";
          manualBox.style.display = "block";
          fetchSection.style.display = "block";
        } else {
          statusEl.textContent = "✗ " + (res.error || "Deploy failed.");
        }
      })
      .catch(e => { statusEl.textContent = "✗ " + e; })
      .finally(() => {
        runBtn.disabled = false;
        runBtn.textContent = "Deploy Script";
      });
  });

  copyCmdBtn.addEventListener("click", () => {
    const text = manualCmdEl.textContent || "";
    if (!text) return;
    navigator.clipboard.writeText(text).then(() => {
      copyCmdBtn.textContent = "Copied!";
      setTimeout(() => { copyCmdBtn.textContent = "Copy"; }, 1500);
    }).catch(() => {
      copyCmdBtn.textContent = "Copy failed";
      setTimeout(() => { copyCmdBtn.textContent = "Copy"; }, 1500);
    });
  });

  fetchBtn.addEventListener("click", () => {
    const conn = lastConn || {
      host:     document.getElementById("t-host").value.trim(),
      username: document.getElementById("t-user").value.trim(),
      password: document.getElementById("t-pass").value,
      port:     document.getElementById("t-port").value || 22,
    };
    if (!conn.host) {
      fetchStatus.textContent = "✗ Target host is required.";
      return;
    }
    fetchBtn.disabled = true;
    fetchBtn.textContent = "Fetching…";
    fetchStatus.textContent = "Looking for the newest result archive on the target…";
    fetchStepsEl.innerHTML = "";
    fetch("/api/test/fetch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(conn),
    })
      .then(r => r.json())
      .then(res => {
        renderSteps(fetchStepsEl, res.steps);
        if (res.ok) {
          fetchStatus.textContent = "✓ " + (res.message || "Fetched.");
          loadResults();
        } else {
          fetchStatus.textContent = "✗ " + (res.error || "Fetch failed.");
        }
      })
      .catch(e => { fetchStatus.textContent = "✗ " + e; })
      .finally(() => {
        fetchBtn.disabled = false;
        fetchBtn.textContent = "Fetch Results";
      });
  });

  refreshBtn.addEventListener("click", loadResults);


  loadResults();
})();
