"""Phase 8: deterministic, single-line fix suggestions for a few well-understood findings.

These are *proposals* only. A suggestion is produced only when the pattern is really present on the
finding's reported line - nothing is ever invented. Anything else returns None (manual or AI-assisted
fixes arrive in Phase 7 and must be marked source="ai-unverified").
"""
from __future__ import annotations

import ast
import posixpath
import re
import warnings
from dataclasses import dataclass
from typing import List, Optional

from models.finding import Finding

SUPPORTED_RULES = {
    "sec-tls-verify-off": "enable TLS certificate verification",
    "sec-debug-enabled": "turn debug mode off",
    "sec-pickle": "use yaml.safe_load instead of yaml.load",
    "sec-hardcoded-secret": "read the secret from an environment variable",
    "sec-eval-exec": "use ast.literal_eval instead of eval",
}


@dataclass
class FixSuggestion:
    new_text: str
    rationale: str
    caution: str = ""


def why_no_fix(finding: Finding, text: Optional[str] = None) -> str:
    if finding.rule_id == "py-syntax":
        return "Syntax errors need a manual fix (or an AI-suggested patch in Phase 7); no safe automatic fix exists."
    if not finding.file or not finding.file.lower().endswith((".py", ".pyw")):
        return "Automatic fixes are only available for Python files in this release."
    if not finding.line:
        return "The finding has no line number, so no precise patch can be generated."
    if finding.rule_id not in SUPPORTED_RULES:
        return f"No automatic fix is available for rule '{finding.rule_id}'."
    return "The expected pattern was not found on the reported line (the file may have changed)."


def can_suggest(finding: Finding) -> bool:
    return bool(finding.file and finding.file.lower().endswith((".py", ".pyw")) and finding.line
                and finding.rule_id in SUPPORTED_RULES)


def _ensure_import(text: str, module: str) -> Optional[str]:
    """Insert ``import <module>`` after the docstring / __future__ imports when missing. None if unparseable."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return None
    for node in tree.body:
        if isinstance(node, ast.Import) and any(a.name == module and not a.asname for a in node.names):
            return text
    insert_after = 0
    for i, node in enumerate(tree.body):
        is_doc = i == 0 and isinstance(node, ast.Expr) and isinstance(getattr(node, "value", None), ast.Constant) \
            and isinstance(node.value.value, str)
        is_future = isinstance(node, ast.ImportFrom) and node.module == "__future__"
        if is_doc or is_future:
            insert_after = node.end_lineno or insert_after
        else:
            break
    lines = text.splitlines(keepends=True)
    eol = "\r\n" if lines and lines[0].endswith("\r\n") else "\n"
    lines.insert(insert_after, f"import {module}{eol}")
    return "".join(lines)


def _replace_on_line(text: str, line_no: int, pattern: "re.Pattern[str]", repl) -> Optional[str]:
    lines = text.splitlines(keepends=True)
    if not (1 <= line_no <= len(lines)):
        return None
    new_line, n = pattern.subn(repl, lines[line_no - 1], count=1)
    if n == 0:
        return None
    lines[line_no - 1] = new_line
    return "".join(lines)


_TLS = re.compile(r"\bverify\s*=\s*False\b")
_DEBUG = re.compile(r"\bDEBUG\s*=\s*True\b")
_YAML = re.compile(r"\byaml\.load\s*\(")
_EVAL = re.compile(r"(?<![\w.])eval\s*\(")
_SECRET = re.compile(r"""^(\s*)([A-Za-z_][A-Za-z0-9_]*)(\s*=\s*)(["'])[^"'\n]+\4(\s*(?:#.*)?)(\r?\n?)$""")


def suggest_fix(finding: Finding, text: str) -> Optional[FixSuggestion]:
    """Return a proposed full-file replacement, or None when no safe fix exists."""
    if not can_suggest(finding):
        return None
    ln = finding.line or 0
    rule = finding.rule_id
    if rule == "sec-tls-verify-off":
        new = _replace_on_line(text, ln, _TLS, "verify=True")
        return new and FixSuggestion(new, "Enable TLS certificate verification (verify=True).",
                                     "Connections to servers with self-signed certificates will now fail; "
                                     "supply a CA bundle path instead of disabling verification.")
    if rule == "sec-debug-enabled":
        new = _replace_on_line(text, ln, _DEBUG, "DEBUG = False")
        return new and FixSuggestion(new, "Turn debug mode off.",
                                     "Make sure development setups can still enable it (e.g. from an environment variable).")
    if rule == "sec-pickle":
        line = (text.splitlines() or [""])[ln - 1] if 1 <= ln <= len(text.splitlines()) else ""
        if "Loader" in line:
            return None
        new = _replace_on_line(text, ln, _YAML, "yaml.safe_load(")
        return new and FixSuggestion(new, "yaml.safe_load cannot construct arbitrary Python objects.",
                                     "Documents that rely on custom YAML tags will no longer load.")
    if rule == "sec-eval-exec":
        new = _replace_on_line(text, ln, _EVAL, "ast.literal_eval(")
        if not new:
            return None
        new = _ensure_import(new, "ast")
        return new and FixSuggestion(new, "ast.literal_eval only evaluates Python literals, so arbitrary code cannot run.",
                                     "Expressions that are not plain literals (calculations, names, calls) will raise ValueError.")
    if rule == "sec-hardcoded-secret":
        lines = text.splitlines(keepends=True)
        if not (1 <= ln <= len(lines)):
            return None
        m = _SECRET.match(lines[ln - 1])
        if not m:
            return None
        indent, name, eq, _q, tail, eol = m.groups()
        lines[ln - 1] = f'{indent}{name}{eq}os.environ.get("{name}", ""){tail}{eol}'
        new = _ensure_import("".join(lines), "os")
        return new and FixSuggestion(new, f'Read {name} from the environment instead of source code.',
                                     "The value already committed must be treated as exposed: rotate it, and set the "
                                     f"{name} environment variable where the program runs.")
    return None
