"""Static checks. Project code is parsed as text/AST only - never imported or run."""
from __future__ import annotations

import ast
import posixpath
import re
import warnings
from typing import Callable, List, Optional, Pattern

from models.finding import Finding
from security.secret_scanner import redact

MAX_LINE = 2000
_COMMENT_PREFIX = ("#", "//", "--", "/*", "*", "<!--")
_TODO = re.compile(r"\b(TODO|FIXME|HACK)\b")
_COMMENT_MARK = re.compile(r"(#|//|/\*|--|<!--|\bREM\b)")
_PLACEHOLDER = re.compile(r"^(<.*>|\$\{.*\}|\{\{.*\}\}|x+|\*+|your[_\-].*|changeme|example.*|none|null|todo)$", re.I)
_SECRET_ASSIGN = re.compile(
    r"""(?i)\b(password|passwd|pwd|secret|api[_\-]?key|token)\b\s*[:=]\s*["']([^"'\s]{4,})["']""")
SOURCE_DOC_EXT = {".md", ".txt", ".rst"}


def _snippet(line: str) -> str:
    return redact(line.strip())[:200]


# --------------------------------------------------------------------------- python syntax
def check_python_syntax(path: str, text: str) -> List[Finding]:
    """Parse with ast.parse (no execution). A failure is a confirmed parser error."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            ast.parse(text, filename=path)
        return []
    except SyntaxError as exc:
        line = exc.lineno
        src = text.splitlines()[line - 1] if line and 0 < line <= len(text.splitlines()) else ""
        return [Finding(
            rule_id="py-syntax", severity="high", category="syntax",
            title=f"Python syntax error: {exc.msg}", file=path, line=line, column=exc.offset,
            evidence=_snippet(src),
            explanation="The Python parser rejected this file. It is parsed with the grammar of the Python "
                        "version running this tool, so code written for another version may be reported.",
            remediation="Fix the reported line; if the project targets a different Python version, "
                        "analyze with that version.",
            source="python-ast", confidence="confirmed", verification="verified-by-parser")]
    except (ValueError, RecursionError, MemoryError) as exc:
        return [Finding(
            rule_id="py-parse-failed", severity="low", category="syntax",
            title="Python file could not be parsed", file=path,
            explanation=f"Parsing aborted ({type(exc).__name__}): {str(exc)[:120]}",
            remediation="Inspect the file manually.", source="python-ast", confidence="medium",
            verification="parser-aborted")]


# --------------------------------------------------------------------------- todo / fixme
def scan_todos(path: str, text: str) -> List[Finding]:
    out: List[Finding] = []
    for no, line in enumerate(text.splitlines(), 1):
        if len(line) > MAX_LINE:
            continue
        m = _TODO.search(line)
        if not m:
            continue
        prefix = line[: m.start()]
        # '*' (block-comment continuation) and "'" (VB comment) only count at the start of the line
        if not (_COMMENT_MARK.search(prefix) or prefix.lstrip().startswith(("*", "'"))):
            continue
        tag = m.group(1)
        out.append(Finding(
            rule_id="todo-marker", severity="info" if tag == "TODO" else "low", category="maintenance",
            title=f"{tag} marker", file=path, line=no, column=m.start() + 1, evidence=_snippet(line),
            explanation=f"A {tag} comment marks unfinished or questionable code.",
            remediation="Resolve it or track it in an issue tracker.", source="todo-scanner",
            confidence="confirmed", verification="verified-by-text-match"))
    return out


# --------------------------------------------------------------------------- security heuristics
class _Rule:
    def __init__(self, rule_id: str, rx: str, severity: str, title: str, why: str, fix: str,
                 exts: Optional[set] = None, code_only: bool = True, flags: int = 0):
        self.rule_id, self.rx = rule_id, re.compile(rx, flags)
        self.severity, self.title, self.why, self.fix = severity, title, why, fix
        self.exts, self.code_only = exts, code_only


PY = {".py", ".pyw"}
JS = {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"}
RULES: List[_Rule] = [
    _Rule("sec-private-key", r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY", "high",
          "Private key committed to the project",
          "Private key material in source control can be extracted by anyone with access.",
          "Remove the key, rotate it, and load keys from a secret store.", code_only=False),
    _Rule("sec-aws-key", r"\bAKIA[0-9A-Z]{16}\b", "high", "Possible AWS access key id",
          "Strings of this shape are AWS access key ids.",
          "Revoke and rotate the key; use environment/role credentials.", code_only=False),
    _Rule("sec-eval-exec", r"(?<![\w.])(?:eval|exec)\s*\(", "medium", "Dynamic code execution (eval/exec)",
          "eval/exec on non-constant input can lead to arbitrary code execution.",
          "Use ast.literal_eval / explicit dispatch instead.", exts=PY | JS),
    _Rule("sec-shell-true", r"subprocess\.\w+\(.*shell\s*=\s*True|\bos\.system\s*\(", "medium",
          "Shell command execution", "Shell invocation with untrusted input enables command injection.",
          "Pass an argument list without shell=True.", exts=PY),
    _Rule("sec-pickle", r"\bpickle\.loads?\s*\(|\byaml\.load\s*\((?!.*Loader)", "medium",
          "Unsafe deserialization", "pickle / yaml.load can execute code while loading untrusted data.",
          "Use json or yaml.safe_load.", exts=PY),
    _Rule("sec-tls-verify-off", r"\bverify\s*=\s*False\b|rejectUnauthorized\s*:\s*false", "medium",
          "TLS certificate verification disabled", "Disabling verification allows man-in-the-middle attacks.",
          "Enable verification and trust the proper CA bundle.", exts=PY | JS),
    _Rule("sec-sql-format", r"""\.execute\w*\(\s*f["']|\.execute\w*\(\s*["'][^"']*["']\s*(?:%|\+)""", "medium",
          "SQL built by string formatting (possible injection)",
          "Concatenated/formatted SQL is a common source of SQL injection.",
          "Use parameterized queries.", exts=PY),
    _Rule("sec-weak-hash", r"hashlib\.(?:md5|sha1)\s*\(", "low", "Weak hash function",
          "MD5/SHA-1 are unsuitable for security purposes.", "Use SHA-256+ (or a password KDF for passwords).",
          exts=PY),
    _Rule("sec-debug-enabled", r"\bDEBUG\s*=\s*True\b", "low", "Debug mode enabled",
          "Debug mode in production leaks internals.", "Disable debug outside development.", exts=PY),
    _Rule("sec-innerhtml", r"\.innerHTML\s*=", "low", "innerHTML assignment (possible XSS)",
          "Assigning unsanitized data to innerHTML enables cross-site scripting.",
          "Use textContent or sanitize the input.", exts=JS),
]


def scan_security(path: str, text: str) -> List[Finding]:
    ext = posixpath.splitext(path)[1].lower()
    out: List[Finding] = []
    is_doc = ext in SOURCE_DOC_EXT
    for no, line in enumerate(text.splitlines(), 1):
        if len(line) > MAX_LINE:
            continue
        stripped = line.strip()
        commented = stripped.startswith(_COMMENT_PREFIX)
        for rule in RULES:
            if rule.exts is not None and ext not in rule.exts:
                continue
            if rule.code_only and (commented or is_doc):
                continue
            m = rule.rx.search(line)
            if m:
                out.append(Finding(
                    rule_id=rule.rule_id, severity=rule.severity, category="security", title=rule.title,
                    file=path, line=no, column=m.start() + 1, evidence=_snippet(line),
                    explanation=rule.why + " (Heuristic pattern match; not a confirmed vulnerability.)",
                    remediation=rule.fix, source="heuristic-security-scan", confidence="medium"
                    if rule.severity != "low" else "low", verification="unverified-heuristic"))
        if not commented and not is_doc:
            m = _SECRET_ASSIGN.search(line)
            if m and not _PLACEHOLDER.match(m.group(2)):
                out.append(Finding(
                    rule_id="sec-hardcoded-secret", severity="medium", category="security",
                    title="Possible hardcoded credential", file=path, line=no, column=m.start() + 1,
                    evidence=_snippet(line),
                    explanation="A password/token/key appears to be assigned a literal value. "
                                "(Heuristic pattern match; value redacted.)",
                    remediation="Move secrets to environment variables or a secret store and rotate this one.",
                    source="heuristic-security-scan", confidence="medium", verification="unverified-heuristic"))
        m = re.search(r"\b[A-Za-z][A-Za-z0-9+.\-]*://[^:/\s@]+:([^@\s]+)@", line)
        if m and not commented and not _PLACEHOLDER.match(m.group(1)):
            out.append(Finding(
                rule_id="sec-credentials-in-url", severity="medium", category="security",
                title="Credentials embedded in a connection URL", file=path, line=no, column=m.start() + 1,
                evidence=_snippet(line),
                explanation="A URL contains user:password. (Heuristic; password redacted.)",
                remediation="Load credentials from the environment and rotate the exposed one.",
                source="heuristic-security-scan", confidence="medium", verification="unverified-heuristic"))
    return out


def analyze_text_file(path: str, text: str) -> List[Finding]:
    """Run all per-file checks that apply to ``path``."""
    ext = posixpath.splitext(path)[1].lower()
    findings: List[Finding] = []
    if ext in (".py", ".pyw"):
        findings += check_python_syntax(path, text)
    if ext not in SOURCE_DOC_EXT:
        findings += scan_todos(path, text)
    findings += scan_security(path, text)
    return findings
