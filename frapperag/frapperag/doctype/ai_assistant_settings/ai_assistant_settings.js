// AI Assistant Settings — client-side form controller
// Renders the Sync Health panel on form refresh (US4).
// Uses frappe.call() exclusively — no fetch, no axios (Constitution JS rule).

frappe.ui.form.on("AI Assistant Settings", {
    refresh(frm) {
        frm.add_custom_button(__("Index All"), () => _trigger_full_index(frm), __("Actions"));
        frm.add_custom_button(__("Refresh Schema Catalog"), () => _trigger_schema_refresh(frm), __("Actions"));
        _sync_install_button(frm);
        _render_active_prefix_banner(frm);
        _render_sync_health(frm);
    },
    embedding_provider(frm) {
        _sync_install_button(frm);
        _render_provider_help(frm);
    },
});

function _sync_install_button(frm) {
    const v = frm.doc.embedding_provider;
    frm.remove_custom_button(__("Install Local Model"), __("Actions"));
    if (v === "e5-small") {
        frm.add_custom_button(__("Install Local Model"),
                              () => _open_install_dialog(frm),
                              __("Actions"));
    }
}

function _render_provider_help(frm) {
    const v = frm.doc.embedding_provider;
    let msg = "";
    if (v === "gemini") {
        msg = __("Gemini (cloud): indexed document text is sent to Google for embedding. Fast and requires no local resources.");
    } else if (v === "e5-small") {
        msg = __("e5-small (local): ~470 MB download, requires ≥2 GB RAM. Indexed text stays on your server — no embedding egress to Google.");
    }
    frm.set_df_property("embedding_provider", "description", msg ||
        "gemini = Google text-embedding-004 (cloud, 768-dim). e5-small = local multilingual-e5-small (384-dim, no egress).");
}

function _render_active_prefix_banner(frm) {
    frappe.call({
        method: "frapperag.api.local_model.get_active_prefix_status",
        callback(r) {
            if (r.exc || !r.message) return;
            const { populated_tables, expected_doctypes } = r.message;
            if (populated_tables.length === 0 && expected_doctypes.length > 0) {
                frm.dashboard.add_indicator(
                    __("Active embedding prefix is empty — run Index All to populate"),
                    "orange"
                );
            }
        },
    });
}

function _open_install_dialog(frm) {
    const dlg = new frappe.ui.Dialog({
        title: __("Install Local Embedding Model"),
        fields: [
            {
                fieldname: "info",
                fieldtype: "HTML",
                options: `<p class="text-muted">${__("Downloads multilingual-e5-small (~470 MB) from HuggingFace and loads it into the sidecar for a test embed. Requires ≥2 GB RAM.")}</p>
                          <p class="text-muted">${__("HF Token is optional — only required for gated models or rate-limited accounts.")}</p>
                          <p class="text-warning">${__("The token is never stored — it is only used for this download.")}</p>`,
            },
            {
                fieldname: "hf_token",
                fieldtype: "Password",
                label: __("HuggingFace Token (optional)"),
            },
        ],
        primary_action_label: __("Install"),
        primary_action({ hf_token }) {
            dlg.set_primary_action(__("Installing…"), null);
            dlg.disable_primary_action();
            dlg.fields_dict.info.$wrapper.html(_build_progress_html());
            frappe.call({
                method: "frapperag.api.local_model.install_local_model",
                args: { hf_token: hf_token || null },
                callback(r) {
                    if (r.exc) {
                        dlg.fields_dict.info.$wrapper.find("#install-status")
                            .html(`<span class="text-danger">${frappe.utils.escape_html(r.exc)}</span>`);
                        dlg.enable_primary_action();
                        dlg.set_primary_action(__("Retry"), dlg.primary_action);
                        return;
                    }
                    _subscribe_install_progress(r.message.job_id, dlg, frm);
                },
            });
        },
    });
    dlg.show();
}

function _build_progress_html() {
    return `
    <div class="progress" style="margin-bottom:8px">
      <div id="install-bar" class="progress-bar progress-bar-striped active"
           role="progressbar" style="width:0%">0%</div>
    </div>
    <p id="install-status" class="text-muted" style="font-size:13px">Queued…</p>
    <pre id="install-log" style="max-height:150px;overflow-y:auto;font-size:11px;background:#f5f5f5;padding:6px;border-radius:3px"></pre>`;
}

function _subscribe_install_progress(job_id, dlg, frm) {
    function handler(data) {
        if (!data || data.job_id !== job_id) return;

        const pct = data.percent || 0;
        const $bar = dlg.fields_dict.info.$wrapper.find("#install-bar");
        const $status = dlg.fields_dict.info.$wrapper.find("#install-status");
        const $log = dlg.fields_dict.info.$wrapper.find("#install-log");

        $bar.css("width", pct + "%").text(pct + "%");
        $status.text(data.message || data.phase || "");
        if (data.message) {
            $log.append(frappe.utils.escape_html(data.message) + "\n");
            $log.scrollTop($log[0].scrollHeight);
        }

        if (data.terminal) {
            frappe.realtime.off("rag_local_model_install_progress", handler);
            if (data.ok) {
                $bar.removeClass("active").addClass("progress-bar-success");
                $status.html(`<span class="text-success">${__("Install successful — click Save to persist Embedding Provider = e5-small, then restart the sidecar and run Index All.")}</span>`);
                dlg.set_primary_action(__("Close"), () => dlg.hide());
                dlg.enable_primary_action();
            } else {
                $bar.removeClass("active").addClass("progress-bar-danger");
                $status.html(`<span class="text-danger">${frappe.utils.escape_html(data.message)}</span>`);
                dlg.set_primary_action(__("Retry"), dlg.primary_action);
                dlg.enable_primary_action();
            }
        }
    }
    frappe.realtime.on("rag_local_model_install_progress", handler);
}

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

function _trigger_schema_refresh(frm) {
    frappe.call({
        method: "frapperag.api.settings.refresh_schema_catalog",
        freeze: true,
        freeze_message: __("Queuing schema catalog refresh…"),
        callback(r) {
            if (r.exc || !r.message) return;

            const status = r.message.status || __("Queued");
            const indicator = r.message.queued ? "blue" : "orange";
            const message = r.message.queued
                ? __("Schema catalog refresh queued.")
                : __("Schema catalog refresh is already {0}.", [status]);

            frappe.show_alert({ message, indicator });
            frm.reload_doc();
        },
    });
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
