// VIOS Resilience Test page logic:
//  - Run test on a target LPAR (deploy + run + download tar.gz)
//  - List downloaded result archives and generated reports
//  - Generate the consolidated Word report from testresult/
(function () {
  const form       = document.getElementById("test-form");
  const runBtn     = document.getElementById("t-run");
  const statusEl   = document.getElementById("t-status");
  const stepsEl    = document.getElementById("t-steps");
  const refreshBtn = document.getElementById("t-refresh");
  const reportBtn  = document.getElementById("t-report");
  const reportStat = document.getElementById("report-status");
  const archivesEl = document.getElementById("archives-list");
  const reportsEl  = document.getElementById("reports-list");

  function humanSize(b) {
    if (b < 1024) return b + " B";
    if (b < 1048576) return (b / 1024).toFixed(1) + " KB";
    return (b / 1048576).toFixed(1) + " MB";
  }

  function renderSteps(steps) {
    stepsEl.innerHTML = "";
    (steps || []).forEach(s => {
      const div = document.createElement("div");
      div.className = "step-row " + (s.ok ? "step-ok" : "step-fail");
      let html = "<strong>" + (s.ok ? "✓" : "✗") + " Step " + s.step +
                 ": " + s.label + "</strong>";
      const body = (s.error && !s.ok) ? s.error : s.output;
      if (body) html += '<div class="step-out">' + escapeHtml(body) + "</div>";
      div.innerHTML = html;
      stepsEl.appendChild(div);
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
        // Archives
        if (!res.archives.length) {
          archivesEl.textContent = "No result archives yet. Run a test above.";
        } else {
          archivesEl.innerHTML = "";
          res.archives.forEach(f => archivesEl.appendChild(resRow(f)));
        }
        // Reports
        if (!res.reports.length) {
          reportsEl.textContent = "No report generated yet.";
        } else {
          reportsEl.innerHTML = "";
          res.reports.forEach(f => reportsEl.appendChild(resRow(f)));
        }
      })
      .catch(e => { archivesEl.textContent = "Error: " + e; });
  }

  function resRow(f) {
    const row = document.createElement("div");
    row.className = "res-row";
    const left = document.createElement("span");
    left.style.fontFamily = "monospace";
    left.textContent = f.name + "  (" + humanSize(f.size) + ")";
    const a = document.createElement("a");
    a.href = "/api/test/download/" + encodeURIComponent(f.name);
    a.textContent = "Download";
    a.className = "btn-plain";
    a.style.textDecoration = "none";
    row.appendChild(left);
    row.appendChild(a);
    return row;
  }

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
    runBtn.disabled = true;
    runBtn.textContent = "Running…";
    statusEl.textContent = "Deploying, running the test and downloading results — this can take several minutes.";
    stepsEl.innerHTML = "";
    fetch("/api/test/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    })
      .then(r => r.json())
      .then(res => {
        renderSteps(res.steps);
        if (res.ok) {
          statusEl.textContent = "✓ " + (res.message || "Test complete.");
          loadResults();
        } else {
          statusEl.textContent = "✗ " + (res.error || "Test failed.");
        }
      })
      .catch(e => { statusEl.textContent = "✗ " + e; })
      .finally(() => {
        runBtn.disabled = false;
        runBtn.textContent = "Run Test";
      });
  });

  reportBtn.addEventListener("click", () => {
    reportBtn.disabled = true;
    reportBtn.textContent = "Generating…";
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
        reportBtn.disabled = false;
        reportBtn.textContent = "Generate Report";
      });
  });

  refreshBtn.addEventListener("click", loadResults);

  loadResults();
})();
