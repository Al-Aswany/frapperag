frappe.pages["rag-admin"].on_page_load = function (wrapper) {
	var page = frappe.ui.make_app_page({
		parent: wrapper,
		title: "Legacy Vector Index Manager",
		single_column: true,
	});

	$(`
		<div class="rag-admin-form" style="padding: 20px;">
			<p class="text-muted" style="max-width:720px; margin-bottom:16px;">
				Legacy vector indexing is for manual compatibility maintenance and v1 fallback support in FrappeAI Assistant.
				Live ERP querying remains the primary structured-data path.
			</p>
			<div class="form-group">
				<label>Legacy Manual Indexing DocType</label>
				<select id="rag-doctype-select" class="form-control" style="max-width:300px;">
					<option value="">-- select --</option>
				</select>
			</div>
			<button id="rag-trigger-btn" class="btn btn-primary">Start Legacy Indexing</button>

			<div id="rag-job-status" style="margin-top:20px; display:none;">
				<p><strong>Job:</strong> <span id="rag-job-id"></span></p>
				<p><strong>Status:</strong> <span id="rag-status"></span></p>
				<div class="progress" style="max-width:400px;">
					<div id="rag-progress-bar" class="progress-bar" role="progressbar"
						style="width:0%">0%</div>
				</div>
				<p style="margin-top:8px; font-size:12px; color:#888;">
					<span id="rag-counts"></span>
				</p>
			</div>

			<div id="rag-job-list" style="margin-top:30px;"></div>
		</div>
	`).appendTo(page.main);

	// Load backend-filtered legacy/manual indexing targets.
	frappe.call({
		method: "frapperag.api.indexer.get_manual_indexing_targets_snapshot",
		callback: function (r) {
			if (!r.message) return;
			var targets = r.message.targets || [];
			targets.forEach(function (dt) {
				$("#rag-doctype-select").append(
					$("<option>").val(dt).text(dt)
				);
			});
		},
	});

	// ─── Trigger indexing ───────────────────────────────────────────────────
	var current_job_id = null;

	$("#rag-trigger-btn").on("click", function () {
		var doctype = $("#rag-doctype-select").val();
		if (!doctype) {
			frappe.msgprint("Please select a legacy manual indexing DocType.");
			return;
		}

		$(this).prop("disabled", true).text("Starting…");

		frappe.call({
			method: "frapperag.api.indexer.trigger_indexing",
			args: { doctype: doctype },
			callback: function (r) {
				current_job_id = r.message.job_id;
				$("#rag-job-id").text(current_job_id);
				$("#rag-status").text(r.message.status);
				$("#rag-job-status").show();
				$("#rag-trigger-btn").prop("disabled", false).text("Start Legacy Indexing");
				subscribe_to_progress();
			},
			error: function () {
				$("#rag-trigger-btn").prop("disabled", false).text("Start Legacy Indexing");
			},
		});
	});

	// ─── Realtime progress (US3) ────────────────────────────────────────────
	var TERMINAL = ["Completed", "Completed with Errors", "Failed", "Failed (Stalled)"];

	function subscribe_to_progress() {
		frappe.realtime.on("rag_index_progress", function (data) {
			if (data.job_id !== current_job_id) return; // guard: multiple tabs
			update_ui(data);
			if (TERMINAL.includes(data.status)) {
				frappe.realtime.off("rag_index_progress");
				frappe.realtime.off("rag_index_error");
				load_job_list();
			}
		});

		frappe.realtime.on("rag_index_error", function (data) {
			if (data.job_id !== current_job_id) return;
			update_ui(data);
			frappe.msgprint({
				message: data.error || "Legacy indexing failed.",
				indicator: "red",
			});
			frappe.realtime.off("rag_index_progress");
			frappe.realtime.off("rag_index_error");
			load_job_list();
		});
	}

	function update_ui(data) {
		var pct = (data.progress_percent || 0).toFixed(1);
		$("#rag-status").text(data.status);
		$("#rag-progress-bar").css("width", pct + "%").text(pct + "%");
		$("#rag-counts").text(
			"Processed: " + (data.processed_records || 0) +
			" | Skipped: "  + (data.skipped_records  || 0) +
			" | Failed: "   + (data.failed_records   || 0) +
			" / Total: "    + (data.total_records    || 0)
		);
	}

	// ─── Job history (US4) ──────────────────────────────────────────────────
	function load_job_list() {
		frappe.call({
			method: "frapperag.api.indexer.list_jobs",
			args: { limit: 10, page: 1 },
			callback: function (r) {
				var rows = (r.message.jobs || []).map(function (j) {
					return (
						"<tr>" +
						"<td>" + j.job_id + "</td>" +
						"<td>" + j.doctype_to_index + "</td>" +
						"<td>" + j.status + "</td>" +
						"<td>" + (j.processed_records || 0) + "/" + (j.total_records || 0) + "</td>" +
						"<td>" + (j.start_time || "") + "</td>" +
						"</tr>"
					);
				}).join("");

				$("#rag-job-list").html(
					"<h5>Recent Legacy Vector Jobs</h5>" +
					"<table class='table table-bordered'>" +
					"<thead><tr>" +
					"<th>Job ID</th><th>DocType</th><th>Status</th>" +
					"<th>Records</th><th>Started</th>" +
					"</tr></thead>" +
					"<tbody>" + rows + "</tbody>" +
					"</table>"
				);
			},
		});
	}

	load_job_list();
};
