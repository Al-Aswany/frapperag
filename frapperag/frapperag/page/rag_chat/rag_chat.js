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

    function build_citations_html(citations_raw) {
        if (!citations_raw) return "";
        try {
            var cites = typeof citations_raw === "string"
                ? JSON.parse(citations_raw) : citations_raw;
            if (!cites || !cites.length) return "";

            var wrapper = document.createElement("div");
            wrapper.style.marginTop = "6px";
            wrapper.style.fontSize  = "11px";

            cites.forEach(function(c) {
                if (c.type === "report_result") {
                    render_report_result(c, wrapper);
                } else {
                    // Existing doc-link behaviour (type === "doc" or type absent)
                    var a = document.createElement("a");
                    a.href        = "/app/" + frappe.router.slug(c.doctype) + "/" + c.name;
                    a.target      = "_blank";
                    a.style.marginRight = "8px";
                    a.style.color       = "#5e64ff";
                    a.textContent = frappe.utils.escape_html(c.doctype) + ": "
                                  + frappe.utils.escape_html(c.name);
                    wrapper.appendChild(a);
                }
            });

            // Return outer HTML so it can be injected into template literals
            // (existing callers concatenate citations_html into a string template)
            return wrapper.outerHTML;
        } catch(e) { return ""; }
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

    function render_message(m, $container) {
        var is_user     = m.role === "user";
        var status_note = m.status === "Pending" ? "<span style='color:#aaa;font-size:11px;display:block;'>(thinking\u2026)</span>"
                        : m.status === "Failed"  ? "<span style='color:red;font-size:11px;display:block;'>(failed)</span>"
                        : "";
        var content_html    = render_content(m.content, is_user);
        var citations_html  = is_user ? "" : build_citations_html(m.citations);
        $(`
            <div class="rag-msg rag-msg-${m.role}" data-id="${m.message_id || ''}"
                 style="margin-bottom:12px; text-align:${is_user ? 'right' : 'left'};">
                <div style="display:inline-block; max-width:75%; padding:10px 14px; border-radius:12px;
                            background:${is_user ? '#5e64ff' : '#f5f5f5'};
                            color:${is_user ? '#fff' : '#333'};">
                    ${content_html}${status_note}${citations_html}
                </div>
            </div>
        `).appendTo($container);
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

    // ── Realtime subscription ─────────────────────────────────────────────────

    function subscribe_realtime(message_id) {
        frappe.realtime.off("rag_chat_response");
        frappe.realtime.on("rag_chat_response", function(data) {
            // Guard: ignore events not belonging to the current in-flight message (FR-014)
            if (data.message_id !== message_id) return;

            $(".rag-pending-bubble").remove();
            var $msgs = $("#rag-messages");

            if (data.status === "Completed") {
                var content_html   = render_content(data.content, false);
                var citations_html = build_citations_html(data.citations);
                $(`
                    <div class="rag-msg rag-msg-assistant" style="margin-bottom:12px;">
                        <div style="display:inline-block; max-width:75%; padding:10px 14px;
                                    border-radius:12px; background:#f5f5f5; color:#333;">
                            ${content_html}${citations_html}
                        </div>
                    </div>
                `).appendTo($msgs);
            } else if (data.status === "Failed") {
                frappe.msgprint({
                    message: "The AI assistant encountered an error. Please try again.",
                    indicator: "red",
                });
            }

            $msgs.scrollTop($msgs[0].scrollHeight);
            set_input_locked(false);
            frappe.realtime.off("rag_chat_response");
            current_message_id = null;
            load_sessions();  // refresh sidebar title after first completed response
        });
    }

    // ── Init ──────────────────────────────────────────────────────────────────
    load_sessions();
};
