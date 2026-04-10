frappe.pages["rag-health"].on_page_load = function (wrapper) {
	var page = frappe.ui.make_app_page({
		parent: wrapper,
		title: "RAG Health",
		single_column: true,
	});

	// Inject structural HTML (matches rag_health.html layout)
	$(`
		<div id="rag-health-page" style="padding: 20px;">

			<div style="display: flex; align-items: center; margin-bottom: 20px;">
				<button id="refresh-btn" class="btn btn-default btn-sm">Refresh</button>
				<span class="last-updated" id="last-updated-ts"></span>
			</div>

			<div class="section-card">
				<h5>Sidecar Status</h5>
				<div id="sidecar-status">
					<span class="status-badge unknown">Loading\u2026</span>
				</div>
			</div>

			<div class="section-card">
				<h5>Indexing Failures &mdash; Last 24 h</h5>
				<table id="index-stats">
					<thead><tr><th>DocType</th><th>Failed Jobs</th></tr></thead>
					<tbody id="index-stats-body">
						<tr><td colspan="2" style="color:#aaa;">Loading\u2026</td></tr>
					</tbody>
				</table>
			</div>

			<div class="section-card">
				<h5>Recent Failed Indexing Jobs</h5>
				<table id="failed-jobs">
					<thead>
						<tr>
							<th>Job ID</th>
							<th>DocType</th>
							<th>Failure Reason</th>
							<th>Time</th>
						</tr>
					</thead>
					<tbody id="failed-jobs-body">
						<tr><td colspan="4" style="color:#aaa;">Loading\u2026</td></tr>
					</tbody>
				</table>
			</div>

			<div class="section-card">
				<h5>Gemini API Status</h5>
				<div id="gemini-status-body" style="font-size: 13px;">Loading\u2026</div>
			</div>

		</div>
	`).appendTo(page.main);

	// ── Render helpers ──────────────────────────────────────────────────────

	function renderSidecarStatus(data) {
		var status = (data.sidecar_status || "Unknown").toLowerCase();
		var label  = data.sidecar_status || "Unknown";
		var ms     = data.sidecar_response_time_ms || 0;
		var detail = status === "reachable"
			? " (" + ms + " ms response time)"
			: "";
		$("#sidecar-status").html(
			'<span class="status-badge ' + status + '">' + label + "</span>" +
			'<span style="font-size:13px; margin-left:8px; color:#555;">' + detail + "</span>"
		);
	}

	function renderIndexStats(data) {
		var failures = data.indexing_failures || [];
		if (!failures.length) {
			$("#index-stats-body").html(
				'<tr><td colspan="2" style="color:#aaa;">No failures in the last 24 h</td></tr>'
			);
			return;
		}
		var rows = failures.map(function (f) {
			return "<tr><td>" + (f.doctype || "") + "</td><td>" + (f.count || 0) + "</td></tr>";
		}).join("");
		$("#index-stats-body").html(rows);
	}

	function renderFailedJobs(jobs) {
		if (!jobs || !jobs.length) {
			$("#failed-jobs-body").html(
				'<tr><td colspan="4" style="color:#aaa;">No failed jobs in the last 24 h</td></tr>'
			);
			return;
		}
		var rows = jobs.map(function (j) {
			return (
				"<tr>" +
				"<td>" + (j.name || "") + "</td>" +
				"<td>" + (j.doctype_to_index || "") + "</td>" +
				'<td class="error-text">' + (j.failure_reason || "&mdash;") + "</td>" +
				"<td>" + (j.end_time || "") + "</td>" +
				"</tr>"
			);
		}).join("");
		$("#failed-jobs-body").html(rows);
	}

	function renderGeminiStatus(data) {
		var lines = [];
		if (data.gemini_last_success) {
			lines.push("<strong>Last success:</strong> " + data.gemini_last_success);
		} else {
			lines.push("<strong>Last success:</strong> <span style='color:#aaa;'>Never</span>");
		}
		if (data.gemini_last_failure) {
			var f = data.gemini_last_failure;
			lines.push(
				"<strong>Last failure:</strong> " + (f.timestamp || "&mdash;") +
				(f.reason ? ' &mdash; <span class="error-text">' + f.reason + "</span>" : "")
			);
		} else {
			lines.push("<strong>Last failure:</strong> <span style='color:#aaa;'>None recorded</span>");
		}
		lines.push("<strong>Chat failures (24 h):</strong> " + (data.chat_failures_24h || 0));
		$("#gemini-status-body").html(lines.join("<br>"));
	}

	// ── Refresh ─────────────────────────────────────────────────────────────

	function refresh() {
		frappe.call({
			method: "frapperag.api.health.get_health_status",
			callback: function (r) {
				if (!r.message) return;
				var data = r.message;
				renderSidecarStatus(data);
				renderIndexStats(data);
				renderGeminiStatus(data);
				loadFailedJobs();
				$("#last-updated-ts").text(
					"Last refreshed: " + frappe.datetime.str_to_user(frappe.datetime.now_datetime())
				);
			},
		});
	}

	function loadFailedJobs() {
		frappe.call({
			method: "frappe.client.get_list",
			args: {
				doctype: "AI Indexing Job",
				filters: [["status", "=", "Failed"]],
				fields: ["name", "doctype_to_index", "failure_reason", "end_time"],
				order_by: "end_time desc",
				limit: 20,
			},
			callback: function (r) {
				renderFailedJobs(r.message || []);
			},
		});
	}

	// ── Boot ────────────────────────────────────────────────────────────────

	$("#refresh-btn").on("click", refresh);
	refresh();
	setInterval(refresh, 30000);
};
