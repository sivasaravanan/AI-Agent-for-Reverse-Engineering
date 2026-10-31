"""Phase 10 - Reports and Delivery plug-in for the AI Software Engineering Agent.

    import report_delivery as rd
    rd.build_reports(report_input_dict, "reports/")                 # HTML + JSON + Markdown (+ section reports)
    rd.build_delivery_package("path/to/app", "delivery.zip")        # secret-scanned, checksummed package
"""
__version__ = "1.0.0"

from .redaction import redact_text, find_secrets  # noqa: E402
from .env_info import collect_environment  # noqa: E402
from .evidence import collect_test_evidence  # noqa: E402
from .sections import normalize_input, build_summary, recommend_next_steps  # noqa: E402
from .render import render_html, render_json, render_markdown, render_section_markdown  # noqa: E402
from .reports import build_reports  # noqa: E402
from .packager import build_delivery_package, PackagingError, PackageOptions  # noqa: E402
from .plugin import PLUGIN_INFO, register, run  # noqa: E402

__all__ = [n for n in dir() if not n.startswith("_")]
