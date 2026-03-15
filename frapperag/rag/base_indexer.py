"""
BaseIndexer ABC — adapted from frappe_assistant_core/core/base_tool.py.

Differences from BaseTool:
- No MCP-specific fields (inputSchema, to_mcp_format, source_app hook registry)
- check_permission(user) takes the user argument explicitly
- log_execution writes to frappe.logger("frapperag") at INFO level
- No _config_cache; settings are always read fresh from the Single DocType
"""

import time
from abc import ABC, abstractmethod

import frappe


class BaseIndexer(ABC):

    name: str = ""
    source_app: str = "frapperag"

    @abstractmethod
    def validate_arguments(self, args: dict) -> None:
        """Raise frappe.ValidationError if args are invalid."""

    @abstractmethod
    def check_permission(self, user: str) -> None:
        """Raise frappe.PermissionError if user is not authorised."""

    @abstractmethod
    def execute(self, args: dict) -> dict:
        """Enqueue job. Return {"job_id": ..., "status": "Queued"}."""

    def safe_execute(self, args: dict, user: str) -> dict:
        """Validate → check permission → execute → log. Returns result dict."""
        start = time.time()
        try:
            self.validate_arguments(args)
            self.check_permission(user)
            result = self.execute(args)
            self.log_execution(args, result, time.time() - start, success=True)
            return result
        except (frappe.PermissionError, frappe.ValidationError):
            self.log_execution(args, {}, time.time() - start, success=False)
            raise
        except Exception:
            self.log_execution(args, {}, time.time() - start, success=False)
            frappe.log_error(
                title=f"RAG Indexer Error [{self.name}]",
                message=frappe.get_traceback(),
            )
            raise

    def log_execution(
        self, _args: dict, _result: dict, duration: float, success: bool
    ) -> None:
        frappe.logger("frapperag").info(
            f"[RAG] {self.name} | success={success} | duration={duration:.2f}s"
        )
