// AI Assistant Settings — client-side form controller
// Renders the Sync Health panel on form refresh (US4).
// Uses frappe.call() exclusively — no fetch, no axios (Constitution JS rule).

frappe.ui.form.on("AI Assistant Settings", {
    refresh(frm) {
        frm.add_custom_button(__("Index All"), () => _trigger_full_index(frm), __("Actions"));
        _render_sync_health(frm);
    },
});

function _trigger_full_index(frm) {
    frappe.confirm(
        __("Queue an indexing job for every allowed DocType? Active jobs will be skipped."),
        () => {
            frappe.call({
                method: "frapperag.api.indexer.trigger_full_index",
                freeze: true,
                freeze_message: __("Queuing indexing jobs…"),
                callback(r) {
                    if (r.exc) return;
                    const { queued = [], skipped = [] } = r.message;
                    const lines = [];
                    if (queued.length) {
                        lines.push(`<b>Queued (${queued.length}):</b> ${queued.map(j => frappe.utils.escape_html(j.doctype)).join(", ")}`);
                    }
                    if (skipped.length) {
                        lines.push(`<b>Skipped — already active (${skipped.length}):</b> ${skipped.map(d => frappe.utils.escape_html(d)).join(", ")}`);
                    }
                    frappe.msgprint({
                        title: __("Index All — Result"),
                        message: lines.join("<br>") || __("Nothing to queue."),
                        indicator: queued.length ? "green" : "orange",
                    });
                },
            });
        }
    );
}

function _render_sync_health(frm) {
    const $field = frm.get_field("sync_health_html");
    if (!$field || !$field.$wrapper) return;

    $field.$wrapper.html("<p class='text-muted'>Loading sync health…</p>");

    frappe.call({
        method: "frapperag.api.indexer.get_sync_health",
        callback(r) {
            if (r.exc || !r.message) {
                $field.$wrapper.html(
                    "<p class='text-danger'>Could not load sync health data.</p>"
                );
                return;
            }
            $field.$wrapper.html(_build_health_html(r.message, frm));
        },
        error() {
            $field.$wrapper.html(
                "<p class='text-danger'>Error fetching sync health.</p>"
            );
        },
    });
}

function _build_health_html(data, frm) {
    const { summary = [], failures = [] } = data;

    let html = "";

    // Summary table
    if (summary.length === 0) {
        html += "<p class='text-muted'>No sync activity in the last 24 hours.</p>";
    } else {
        html += `
        <h6>Last 24 Hours — Per DocType Summary</h6>
        <table class='table table-bordered table-condensed' style='margin-bottom:16px'>
          <thead>
            <tr>
              <th>DocType</th>
              <th>Success</th>
              <th>Failed</th>
              <th>Last Success</th>
            </tr>
          </thead>
          <tbody>
            ${summary.map(row => `
              <tr>
                <td>${frappe.utils.escape_html(row.doctype_name)}</td>
                <td>${row.success_count || 0}</td>
                <td style='color:${row.failed_count > 0 ? "red" : "inherit"}'>${row.failed_count || 0}</td>
                <td>${row.last_success ? frappe.utils.escape_html(row.last_success) : "—"}</td>
              </tr>`).join("")}
          </tbody>
        </table>`;
    }

    // Failures list
    if (failures.length === 0) {
        html += "<p class='text-success'>No failed sync entries.</p>";
    } else {
        html += `
        <h6>Failed Sync Entries (all time, up to 100)</h6>
        <table class='table table-bordered table-condensed'>
          <thead>
            <tr>
              <th>Log ID</th>
              <th>DocType</th>
              <th>Record</th>
              <th>Trigger</th>
              <th>Error</th>
              <th>Created</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            ${failures.map(f => `
              <tr>
                <td><small>${frappe.utils.escape_html(f.sync_log_id)}</small></td>
                <td>${frappe.utils.escape_html(f.doctype_name)}</td>
                <td>${frappe.utils.escape_html(f.record_name)}</td>
                <td>${frappe.utils.escape_html(f.trigger_type)}</td>
                <td><small style='color:red'>${frappe.utils.escape_html((f.error_message || "").substring(0, 120))}</small></td>
                <td><small>${frappe.utils.escape_html(f.creation)}</small></td>
                <td>
                  <button class='btn btn-xs btn-warning rag-retry-btn'
                          data-sync-log-id='${frappe.utils.escape_html(f.sync_log_id)}'>
                    Retry
                  </button>
                </td>
              </tr>`).join("")}
          </tbody>
        </table>`;
    }

    // Attach retry button handlers after DOM insertion via a small wrapper
    // so we can re-bind after innerHTML is replaced.
    setTimeout(() => {
        const $field = frm.get_field("sync_health_html");
        if (!$field || !$field.$wrapper) return;
        $field.$wrapper.find(".rag-retry-btn").on("click", function () {
            const syncLogId = $(this).data("sync-log-id");
            _retry_sync(syncLogId, frm);
        });
    }, 50);

    return html;
}

function _retry_sync(sync_log_id, frm) {
    frappe.call({
        method: "frapperag.api.indexer.retry_sync",
        args: { sync_log_id },
        callback(r) {
            if (r.exc) {
                frappe.msgprint({
                    title: "Retry Failed",
                    message: r.exc,
                    indicator: "red",
                });
                return;
            }
            frappe.show_alert({
                message: `Retry queued: ${r.message.sync_log_id}`,
                indicator: "green",
            }, 4);
            // Refresh the health panel to show the new Queued entry
            _render_sync_health(frm);
        },
    });
}
