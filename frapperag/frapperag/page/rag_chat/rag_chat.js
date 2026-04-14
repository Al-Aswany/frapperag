frappe.pages["rag-chat"].on_page_load = function(wrapper) {
    var page = frappe.ui.make_app_page({
        parent: wrapper,
        title: "AI Assistant",
        single_column: true,
    });

    $(`
        <div class="rag-chat-layout" style="display:flex; height:calc(100vh - 100px);">
            <div class="rag-sessions" style="width:260px; border-right:1px solid #eee; overflow-y:auto; padding:12px;">
                <button id="rag-new-session" class="btn btn-sm btn-primary" style="width:100%; margin-bottom:8px;">New Chat</button>
                <div id="rag-session-list"></div>
            </div>
            <div class="rag-thread" style="flex:1; display:flex; flex-direction:column;">
                <div id="rag-messages" style="flex:1; overflow-y:auto; padding:16px;"></div>
                <div style="padding:12px; border-top:1px solid #eee; display:flex; gap:8px;">
                    <input id="rag-input" type="text" class="form-control"
                           placeholder="Ask a question about your data\u2026" style="flex:1;" disabled />
                    <button id="rag-send" class="btn btn-primary" disabled>Send</button>
                </div>
            </div>
        </div>
    `).appendTo(page.main);

    var current_session_id = null;
    var current_message_id = null;

    // ── Session list ──────────────────────────────────────────────────────────

    function load_sessions() {
        frappe.call({
            method: "frapperag.api.chat.list_sessions",
            args: { include_archived: 0 },
            callback: function(r) {
                var sessions = r.message.sessions || [];
                var $list = $("#rag-session-list").empty();
                sessions.forEach(function(s) {
                    var active = s.session_id === current_session_id ? "background:#f0f4ff;" : "";
                    $(`
                        <div class="rag-session-item" data-id="${s.session_id}"
                             style="padding:8px; cursor:pointer; border-radius:4px; margin-bottom:4px;
                                    display:flex; justify-content:space-between; align-items:center; ${active}">
                            <span style="overflow:hidden; text-overflow:ellipsis; white-space:nowrap; max-width:180px;">
                                ${frappe.utils.escape_html(s.title || "New Chat")}
                            </span>
                            <button class="btn btn-xs btn-default rag-archive-btn"
                                    data-id="${s.session_id}" title="Archive">\u22ef</button>
                        </div>
                    `).appendTo($list);
                });
            }
        });
    }

    // ── Message thread ────────────────────────────────────────────────────────

    function load_messages(session_id) {
        frappe.call({
            method: "frapperag.api.chat.get_messages",
            args: { session_id: session_id },
            callback: function(r) {
                var messages = r.message.messages || [];
                var $msgs = $("#rag-messages").empty();
                messages.forEach(function(m) { render_message(m, $msgs); });
                $msgs.scrollTop($msgs[0].scrollHeight);
                // Re-lock if a Pending message exists (e.g., page reload mid-job)
                var pending = messages.find(function(m) { return m.status === "Pending"; });
                set_input_locked(!!pending);
                if (pending) {
                    current_message_id = pending.message_id;
                    subscribe_realtime(current_message_id);
                }
            }
        });
    }

    function render_content(text, is_user) {
        if (is_user) return frappe.utils.escape_html(text || "");
        // Assistant messages: render markdown. frappe.markdown uses marked+sanitize.
        return frappe.markdown ? frappe.markdown(text || "") : frappe.utils.escape_html(text || "").replace(/\n/g, "<br>");
    }

    // Render one citation item into a container element.
    function render_citation(c, container) {
        if (c.type === "report_result") {
            render_report_result(c, container);
        } else if (c.type === "query_result") {
            render_query_result(c, container);
        } else if (c.type === "record_detail") {
            render_record_detail(c, container);
        } else {
            // Fallback doc-link chip (type === "doc" or absent)
            var a = document.createElement("a");
            a.href        = "/app/" + frappe.router.slug(c.doctype) + "/" + c.name;
            a.target      = "_blank";
            a.style.marginRight = "8px";
            a.style.color       = "#5e64ff";
            a.textContent = frappe.utils.escape_html(c.doctype) + ": "
                          + frappe.utils.escape_html(c.name);
            container.appendChild(a);
        }
    }

    // Returns a DOM element for the citation strip, or null when there is nothing
    // to render. Citations are only tool-call results (report_result, query_result,
    // record_detail) — retriever candidates are stored in context_sources and never
    // shown here. At most 5 items are visible; extras collapse behind "+N more".
    function build_citations_el(citations_raw) {
        if (!citations_raw) return null;
        try {
            var cites = typeof citations_raw === "string"
                ? JSON.parse(citations_raw) : citations_raw;
            if (!cites || !cites.length) return null;

            var VISIBLE_CAP = 5;
            var visible = cites.slice(0, VISIBLE_CAP);
            var hidden  = cites.slice(VISIBLE_CAP);

            var wrapper = document.createElement("div");
            wrapper.style.marginTop = "6px";
            wrapper.style.fontSize  = "11px";

            visible.forEach(function(c) { render_citation(c, wrapper); });

            if (hidden.length) {
                var extra = document.createElement("div");
                extra.style.display = "none";
                hidden.forEach(function(c) { render_citation(c, extra); });

                var toggle = document.createElement("span");
                toggle.style.cursor   = "pointer";
                toggle.style.color    = "#5e64ff";
                toggle.style.fontSize = "11px";
                toggle.style.display  = "block";
                toggle.style.marginTop = "4px";
                toggle.textContent    = "+" + hidden.length + " more";
                toggle.addEventListener("click", function() {
                    var expanded = extra.style.display !== "none";
                    extra.style.display = expanded ? "none" : "";
                    toggle.textContent  = expanded
                        ? "+" + hidden.length + " more"
                        : "Show less";
                });

                wrapper.appendChild(toggle);
                wrapper.appendChild(extra);
            }

            return wrapper;
        } catch(e) { return null; }
    }

    function render_report_result(c, container) {
        var wrapper = document.createElement("div");
        wrapper.className = "rag-report-result";
        wrapper.style.marginTop  = "8px";
        wrapper.style.overflowX  = "auto";

        // Report name heading
        var title = document.createElement("p");
        title.className   = "rag-report-name";
        title.style.fontWeight  = "600";
        title.style.marginBottom = "4px";
        title.textContent = c.report_name || "Report";
        wrapper.appendChild(title);

        // Table
        var table = document.createElement("table");
        table.className          = "rag-report-table";
        table.style.borderCollapse = "collapse";
        table.style.width          = "100%";
        table.style.fontSize       = "11px";

        // Header
        var thead     = document.createElement("thead");
        var headerRow = document.createElement("tr");
        (c.columns || []).forEach(function(col) {
            var th = document.createElement("th");
            th.style.borderBottom  = "1px solid #ccc";
            th.style.padding       = "3px 6px";
            th.style.textAlign     = "left";
            th.style.whiteSpace    = "nowrap";
            th.textContent = String(col);
            headerRow.appendChild(th);
        });
        thead.appendChild(headerRow);
        table.appendChild(thead);

        // Body rows
        var tbody = document.createElement("tbody");
        (c.rows || []).forEach(function(row) {
            var tr = document.createElement("tr");
            (row || []).forEach(function(cell) {
                var td = document.createElement("td");
                td.style.padding      = "2px 6px";
                td.style.borderBottom = "1px solid #eee";
                td.textContent = (cell === null || cell === undefined)
                    ? "" : String(cell);
                tr.appendChild(td);
            });
            tbody.appendChild(tr);
        });
        table.appendChild(tbody);
        wrapper.appendChild(table);

        // Truncation note (FR-019)
        var rowsShown = (c.rows || []).length;
        if (c.row_count > rowsShown) {
            var note = document.createElement("p");
            note.className   = "rag-report-truncation-note";
            note.style.color = "#888";
            note.style.marginTop = "4px";
            note.textContent = "Showing " + rowsShown + " of " + c.row_count + " rows.";
            wrapper.appendChild(note);
        }

        container.appendChild(wrapper);
    }

    function render_query_result(c, container) {
        // Same table shape as render_report_result — columns + rows + optional truncation note.
        // No redundant heading: the assistant narrative already describes the query.
        var wrapper = document.createElement("div");
        wrapper.style.marginTop = "8px";
        wrapper.style.overflowX = "auto";

        var table = document.createElement("table");
        table.style.borderCollapse = "collapse";
        table.style.width          = "100%";
        table.style.fontSize       = "11px";

        var thead = document.createElement("thead");
        var headerRow = document.createElement("tr");
        (c.columns || []).forEach(function(col) {
            var th = document.createElement("th");
            th.style.borderBottom = "1px solid #ccc";
            th.style.padding      = "3px 6px";
            th.style.textAlign    = "left";
            th.style.whiteSpace   = "nowrap";
            th.textContent = String(col);
            headerRow.appendChild(th);
        });
        thead.appendChild(headerRow);
        table.appendChild(thead);

        var tbody = document.createElement("tbody");
        (c.rows || []).forEach(function(row) {
            var tr = document.createElement("tr");
            (row || []).forEach(function(cell) {
                var td = document.createElement("td");
                td.style.padding      = "2px 6px";
                td.style.borderBottom = "1px solid #eee";
                td.textContent = (cell === null || cell === undefined) ? "" : String(cell);
                tr.appendChild(td);
            });
            tbody.appendChild(tr);
        });
        table.appendChild(tbody);
        wrapper.appendChild(table);

        var rowsShown = (c.rows || []).length;
        if (c.row_count > rowsShown) {
            var note = document.createElement("p");
            note.style.color     = "#888";
            note.style.marginTop = "4px";
            note.style.fontSize  = "11px";
            note.textContent = "Showing " + rowsShown + " of " + c.row_count + " rows.";
            wrapper.appendChild(note);
        }

        container.appendChild(wrapper);
    }

    function render_record_detail(c, container) {
        // Key-value card for a single Frappe document.
        // Header fields render as a clean key-value grid.
        // The "items" key (if it is a structured array) renders as a line-item table.
        var wrapper = document.createElement("div");
        wrapper.style.marginTop  = "8px";
        wrapper.style.fontSize   = "11px";
        wrapper.style.overflowX  = "auto";

        // Heading: linked doctype + name
        var heading = document.createElement("p");
        heading.style.fontWeight   = "600";
        heading.style.marginBottom = "4px";
        var link = document.createElement("a");
        link.href        = "/app/" + frappe.router.slug(c.doctype) + "/" + c.name;
        link.target      = "_blank";
        link.style.color = "#5e64ff";
        link.textContent = frappe.utils.escape_html(c.doctype) + ": "
                         + frappe.utils.escape_html(c.name);
        heading.appendChild(link);
        wrapper.appendChild(heading);

        var fields = c.fields || {};

        // --- Header key-value card (scalar fields only) ---
        var headerTable = document.createElement("table");
        headerTable.style.borderCollapse = "collapse";
        headerTable.style.width          = "100%";
        headerTable.style.marginBottom   = "6px";
        var headerTbody = document.createElement("tbody");

        Object.keys(fields).forEach(function(key) {
            if (key === "items") return;                          // handled separately
            var val = fields[key];
            if (val === null || val === undefined || val === "") return;
            if (Array.isArray(val) || (typeof val === "object")) return;

            var label = key.replace(/_/g, " ")
                           .replace(/\b\w/g, function(l) { return l.toUpperCase(); });

            var tr = document.createElement("tr");

            var tdKey = document.createElement("td");
            tdKey.style.padding      = "2px 6px";
            tdKey.style.borderBottom = "1px solid #eee";
            tdKey.style.color        = "#888";
            tdKey.style.whiteSpace   = "nowrap";
            tdKey.style.verticalAlign = "top";
            tdKey.style.width        = "35%";
            tdKey.textContent = label;

            var tdVal = document.createElement("td");
            tdVal.style.padding      = "2px 6px";
            tdVal.style.borderBottom = "1px solid #eee";
            tdVal.style.wordBreak    = "break-word";
            tdVal.textContent = String(val);

            tr.appendChild(tdKey);
            tr.appendChild(tdVal);
            headerTbody.appendChild(tr);
        });
        headerTable.appendChild(headerTbody);
        if (headerTbody.children.length) wrapper.appendChild(headerTable);

        // --- Items table (structured array) ---
        var items = fields["items"];
        if (Array.isArray(items) && items.length) {
            // Collect union of keys across all rows to build dynamic columns,
            // but use a preferred display order when the keys are known.
            var ITEM_COL_ORDER = ["item_code", "item_name", "qty", "rate",
                                  "amount", "basic_rate", "uom",
                                  "s_warehouse", "t_warehouse"];
            var keySet = {};
            items.forEach(function(row) { Object.keys(row).forEach(function(k) { keySet[k] = 1; }); });
            var cols = ITEM_COL_ORDER.filter(function(k) { return keySet[k]; });
            // Append any remaining keys not in the preferred list
            Object.keys(keySet).forEach(function(k) { if (cols.indexOf(k) === -1) cols.push(k); });

            var colLabels = {
                item_code:   "Item Code",
                item_name:   "Item",
                qty:         "Qty",
                rate:        "Rate",
                amount:      "Amount",
                basic_rate:  "Rate",
                uom:         "UOM",
                s_warehouse: "From Warehouse",
                t_warehouse: "To Warehouse",
            };

            var itemSection = document.createElement("div");
            itemSection.style.marginTop = "6px";

            var itemTitle = document.createElement("p");
            itemTitle.style.fontWeight   = "600";
            itemTitle.style.marginBottom = "4px";
            itemTitle.style.color        = "#555";
            itemTitle.textContent = "Items (" + items.length + ")";
            itemSection.appendChild(itemTitle);

            var itemTable = document.createElement("table");
            itemTable.style.borderCollapse = "collapse";
            itemTable.style.width          = "100%";
            itemTable.style.fontSize       = "11px";

            // thead
            var thead = document.createElement("thead");
            var headerRow = document.createElement("tr");
            cols.forEach(function(col) {
                var th = document.createElement("th");
                th.style.borderBottom  = "2px solid #ccc";
                th.style.padding       = "3px 6px";
                th.style.textAlign     = "left";
                th.style.whiteSpace    = "nowrap";
                th.style.color         = "#555";
                th.textContent = colLabels[col] || col.replace(/_/g, " ")
                                                       .replace(/\b\w/g, function(l) { return l.toUpperCase(); });
                headerRow.appendChild(th);
            });
            thead.appendChild(headerRow);
            itemTable.appendChild(thead);

            // tbody
            var itemTbody = document.createElement("tbody");
            items.forEach(function(row) {
                var tr = document.createElement("tr");
                cols.forEach(function(col) {
                    var td = document.createElement("td");
                    td.style.padding      = "2px 6px";
                    td.style.borderBottom = "1px solid #eee";
                    td.style.whiteSpace   = (col === "item_name") ? "normal" : "nowrap";
                    var v = row[col];
                    td.textContent = (v === null || v === undefined) ? "" : String(v);
                    tr.appendChild(td);
                });
                itemTbody.appendChild(tr);
            });
            itemTable.appendChild(itemTbody);
            itemSection.appendChild(itemTable);
            wrapper.appendChild(itemSection);
        }

        container.appendChild(wrapper);
    }

    function render_message(m, $container) {
        var is_user     = m.role === "user";
        var status_note = m.status === "Pending" ? "<span style='color:#aaa;font-size:11px;display:block;'>(thinking\u2026)</span>"
                        : m.status === "Failed"  ? "<span style='color:red;font-size:11px;display:block;'>(failed)</span>"
                        : "";
        var content_html = render_content(m.content, is_user);
        var $msg = $(`
            <div class="rag-msg rag-msg-${m.role}" data-id="${m.message_id || ''}"
                 style="margin-bottom:12px; text-align:${is_user ? 'right' : 'left'};">
                <div style="display:inline-block; max-width:75%; padding:10px 14px; border-radius:12px;
                            background:${is_user ? '#5e64ff' : '#f5f5f5'};
                            color:${is_user ? '#fff' : '#333'};">
                    ${content_html}${status_note}
                </div>
            </div>
        `);
        if (!is_user) {
            var cites_el = build_citations_el(m.citations);
            if (cites_el) $msg.find("> div").append(cites_el);
        }
        $msg.appendTo($container);
    }

    function set_input_locked(locked) {
        $("#rag-input, #rag-send").prop("disabled", !!locked);
    }

    // ── New session ───────────────────────────────────────────────────────────

    $("#rag-new-session").on("click", function() {
        frappe.call({
            method: "frapperag.api.chat.create_session",
            callback: function(r) {
                current_session_id = r.message.session_id;
                current_message_id = null;
                frappe.realtime.off("rag_chat_response");
                $("#rag-messages").empty();
                set_input_locked(false);
                load_sessions();
                $("#rag-input").focus();
            }
        });
    });

    // ── Session click ─────────────────────────────────────────────────────────

    $(document).on("click", ".rag-session-item", function(e) {
        if ($(e.target).hasClass("rag-archive-btn")) return;
        var sid = $(this).data("id");
        if (sid === current_session_id) return;
        current_session_id = sid;
        current_message_id = null;
        frappe.realtime.off("rag_chat_response");
        load_sessions();
        load_messages(sid);
    });

    // ── Archive ───────────────────────────────────────────────────────────────

    $(document).on("click", ".rag-archive-btn", function(e) {
        e.stopPropagation();
        var sid = $(this).data("id");
        frappe.confirm("Archive this chat session?", function() {
            frappe.call({
                method: "frapperag.api.chat.archive_session",
                args: { session_id: sid },
                callback: function() {
                    if (sid === current_session_id) {
                        current_session_id = null;
                        current_message_id = null;
                        frappe.realtime.off("rag_chat_response");
                        $("#rag-messages").empty();
                        set_input_locked(true);
                    }
                    load_sessions();
                }
            });
        });
    });

    // ── Send message ──────────────────────────────────────────────────────────

    function send_message() {
        var content = $("#rag-input").val().trim();
        if (!content || !current_session_id) return;
        set_input_locked(true);
        $("#rag-input").val("");

        // Optimistic user bubble
        var $msgs = $("#rag-messages");
        render_message({role: "user", content: content, status: "Completed"}, $msgs);

        // Pending assistant bubble
        var $pending = $(
            "<div class='rag-msg rag-msg-assistant rag-pending-bubble' style='margin-bottom:12px;'>" +
            "<div style='display:inline-block; padding:10px 14px; border-radius:12px; background:#f5f5f5; color:#aaa;'>" +
            "Thinking\u2026</div></div>"
        ).appendTo($msgs);
        $msgs.scrollTop($msgs[0].scrollHeight);

        frappe.call({
            method: "frapperag.api.chat.send_message",
            args: { session_id: current_session_id, content: content },
            callback: function(r) {
                current_message_id = r.message.message_id;
                subscribe_realtime(current_message_id);
            },
            error: function() {
                $pending.remove();
                set_input_locked(false);
            }
        });
    }

    $("#rag-send").on("click", send_message);
    $("#rag-input").on("keydown", function(e) {
        if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send_message(); }
    });

    // ── Realtime subscription + polling fallback ──────────────────────────────
    //
    // Two paths resolve the response:
    //   1. Realtime: frappe.publish_realtime fires rag_chat_response from the worker.
    //   2. Poll: setInterval checks get_message_status every 2 s for up to 60 s.
    // Whichever path fires first wins; the `handled` flag prevents double-render.

    function subscribe_realtime(message_id) {
        var handled      = false;
        var poll_timer   = null;
        var poll_count   = 0;
        var POLL_MAX     = 30;   // 30 ticks × 2 s = 60 s ceiling

        function stop_poll() {
            if (poll_timer) {
                clearInterval(poll_timer);
                poll_timer = null;
            }
        }

        function handle_response(source, data) {
            // Guard 1: wrong message (stale event from a previous send)
            if (data.message_id !== message_id) return;
            // Guard 2: already handled by the other path
            if (handled) return;
            handled = true;
            stop_poll();

            console.log("[rag-chat] response via " + source + " — status=" + data.status, data);

            $(".rag-pending-bubble").remove();
            var $msgs = $("#rag-messages");

            if (data.status === "Completed") {
                var content_html = render_content(data.content, false);
                var $bubble = $(`
                    <div class="rag-msg rag-msg-assistant" style="margin-bottom:12px;">
                        <div style="display:inline-block; max-width:75%; padding:10px 14px;
                                    border-radius:12px; background:#f5f5f5; color:#333;">
                            ${content_html}
                        </div>
                    </div>
                `);
                var cites_el = build_citations_el(data.citations);
                if (cites_el) $bubble.find("> div").append(cites_el);
                $bubble.appendTo($msgs);
            } else if (data.status === "Failed") {
                var err_text = data.failure_reason
                    || "The AI assistant encountered an error. Please try again.";
                $(`
                    <div class="rag-msg rag-msg-assistant" style="margin-bottom:12px;">
                        <div style="display:inline-block; max-width:75%; padding:10px 14px;
                                    border-radius:12px; background:#fff0f0; color:#c00;">
                            ${frappe.utils.escape_html(err_text)}
                        </div>
                    </div>
                `).appendTo($msgs);
            }

            $msgs.scrollTop($msgs[0].scrollHeight);
            set_input_locked(false);
            frappe.realtime.off("rag_chat_response");
            current_message_id = null;
            load_sessions();  // refresh sidebar title after first completed response
        }

        // Path 1 — realtime
        frappe.realtime.off("rag_chat_response");
        frappe.realtime.on("rag_chat_response", function(data) {
            console.log("[rag-chat] realtime rag_chat_response received:", data);
            handle_response("realtime", data);
        });

        // Path 2 — polling fallback (every 2 s, max 60 s)
        poll_timer = setInterval(function() {
            if (handled) { stop_poll(); return; }

            poll_count++;
            if (poll_count > POLL_MAX) {
                console.warn("[rag-chat] poll timed out after 60 s for message_id=" + message_id);
                stop_poll();
                return;
            }

            console.log("[rag-chat] poll #" + poll_count + " — message_id=" + message_id);
            frappe.call({
                method: "frapperag.api.chat.get_message_status",
                args: { message_id: message_id },
                callback: function(r) {
                    var status = r.message && r.message.status;
                    console.log("[rag-chat] poll #" + poll_count + " result: status=" + status);
                    if (r.message && status !== "Pending") {
                        handle_response("poll", r.message);
                    }
                }
            });
        }, 2000);
    }

    // ── Stalled-message realtime (from scheduler sweep) ───────────────────────
    // Handles chat_message_update events emitted by mark_stalled_chat_messages()
    // when a Pending message exceeds the 10-minute processing timeout.

    frappe.realtime.on("chat_message_update", function(data) {
        if (data.message_id !== current_message_id) return;
        if (data.status !== "Failed") return;

        $(".rag-pending-bubble").remove();
        var $msgs = $("#rag-messages");
        var err_text = data.failure_reason || "Response timed out. Please try again.";
        $(`
            <div class="rag-msg rag-msg-assistant" style="margin-bottom:12px;">
                <div style="display:inline-block; max-width:75%; padding:10px 14px;
                            border-radius:12px; background:#fff0f0; color:#c00;">
                    ${frappe.utils.escape_html(err_text)}
                </div>
            </div>
        `).appendTo($msgs);
        $msgs.scrollTop($msgs[0].scrollHeight);
        set_input_locked(false);
        frappe.realtime.off("rag_chat_response");
        current_message_id = null;
    });

    // ── Init ──────────────────────────────────────────────────────────────────
    load_sessions();
};
