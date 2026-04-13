"""
bench execute relay for FrappeRAG v1.0 Chat Quality runner.

The actual runner lives at specs/005-v1-chat-quality/runner.py but
`bench execute` cannot handle hyphens in directory names — this relay
loads the runner by file path.

Usage:
    # Pre-flight check only:
    bench --site golive.site1 execute frapperag.v1_runner.main \
        --kwargs "{'check_only': True}"

    # Full run:
    bench --site golive.site1 execute frapperag.v1_runner.main
"""

import importlib.util
import os


def _load_runner():
    runner_path = os.path.normpath(
        os.path.join(
            os.path.dirname(__file__),
            "../specs/005-v1-chat-quality/runner.py",
        )
    )
    spec = importlib.util.spec_from_file_location("v1_runner_impl", runner_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main(check_only=False):
    _load_runner().main(check_only=check_only)
