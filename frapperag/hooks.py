from frapperag.rag.legacy_vector_policy import LEGACY_VECTOR_DOCTYPES

app_name      = "frapperag"
app_title     = "FrappeAI Assistant"
app_publisher = "Mahmoud Hussein"
app_description = "AI assistant for Frappe / ERPNext with optional legacy vector compatibility"
app_email     = "mahmudhussain2001ab@gmail.com"
app_license   = "mit"

after_install = "frapperag.setup.install.after_install"
after_migrate = "frapperag.setup.install.after_migrate"

fixtures = [
    {"dt": "Role", "filters": [["name", "in", ["RAG Admin", "RAG User"]]]},
]

scheduler_events = {
    "all": [
        "frapperag.rag.health.run_health_check",
    ],
    "cron": {
        "*/5 * * * *": [
            "frapperag.rag.indexer.mark_stalled_jobs",
            "frapperag.rag.chat_runner.mark_stalled_chat_messages",
            "frapperag.rag.sync_runner.mark_stalled_sync_jobs",
        ],
    },
    "daily": [
        "frapperag.rag.sync_runner.prune_sync_event_log",
    ],
}

doc_events = {
    doctype: {
        "on_update": "frapperag.rag.sync_hooks.on_document_save",
        "after_rename": "frapperag.rag.sync_hooks.on_document_rename",
        "on_trash": "frapperag.rag.sync_hooks.on_document_trash",
    }
    for doctype in LEGACY_VECTOR_DOCTYPES
}

permission_query_conditions = {
    "Chat Session": "frapperag.frapperag.doctype.chat_session.chat_session.permission_query_conditions",
    "Chat Message": "frapperag.frapperag.doctype.chat_message.chat_message.permission_query_conditions",
}
