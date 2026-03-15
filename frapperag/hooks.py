app_name      = "frapperag"
app_title     = "FrappeRAG"
app_publisher = "Mahmoud Hussein"
app_description = "RAG embedding pipeline for Frappe / ERPNext"
app_email     = "mahmudhussain2001ab@gmail.com"
app_license   = "mit"

after_install = "frapperag.setup.install.after_install"

fixtures = [
    {"dt": "Role", "filters": [["name", "in", ["RAG Admin", "RAG User"]]]},
]

scheduler_events = {
    "cron": {
        "*/30 * * * *": [
            "frapperag.rag.indexer.mark_stalled_jobs"
        ],
    }
}
