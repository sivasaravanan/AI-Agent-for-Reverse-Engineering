"""Built-in analyzers for HTML, CSS, PHP and Go (master prompt language targets).

* HTML/CSS: standard-library structural checks (heuristic - labelled as such, never "compiler errors").
* PHP: ``php -l`` (syntax only, nothing is run) when PHP is installed, plus a few heuristic security rules.
* Go: ``gofmt -e`` (parse only, via stdin) when installed, plus heuristics.
Missing tools => status "unavailable" with an install hint. Nothing is installed or executed from the project.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from html.parser import HTMLParser
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Tuple

from analyzers.base import BaseAnalyzer, ToolResult
from models.finding import Finding

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
OPTIONAL_CLOSE = {"p", "li", "dt", "dd", "tr", "td", "th", "thead", "tbody", "tfoot", "option", "optgroup", "colgroup", "html", "head", "body"}


def _scratch_dir():
    """Temporary copies go to the scratch folder chosen in Storage & clean-up (None = the system temp folder)."""
    try:
        from plugins.build_execution.storage import temp_root
        return temp_root()
    except Exception:  # noqa: BLE001 - never a reason to fail
        return None

def _f(rule, sev, cat, title, file, line, evidence, expl, fix, source, conf="medium", ver="unverified-heuristic") -> Finding:
    return Finding(rule_id=rule, severity=sev, category=cat, title=title, file=file, line=line, evidence=evidence[:300],
                   explanation=expl, remediation=fix, source=source, confidence=conf, verification=ver)


def _run(cmd: List[str], inp: Optional[str] = None, timeout: int = 20, cwd: Optional[str] = None) -> Tuple[int, str, str]:
    p = subprocess.run(cmd, input=inp, capture_output=True, text=True, timeout=timeout, stdin=None if inp is not None else subprocess.DEVNULL,
                       cwd=cwd)
    return p.returncode, p.stdout, p.stderr


# ------------------------------------------------------------------------------------------------ HTML
class _Html(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: List[Tuple[str, int]] = []
        self.issues: List[Tuple[str, int, str]] = []
        self.ids: Dict[str, int] = {}
        self.has_title = self.has_lang = self.has_html = False

    def handle_starttag(self, tag, attrs):
        line = self.getpos()[0]
        a = dict(attrs)
        if tag == "html":
            self.has_html = True
            self.has_lang = bool(a.get("lang"))
        if tag == "title":
            self.has_title = True
        if "id" in a and a["id"]:
            if a["id"] in self.ids:
                self.issues.append(("html-duplicate-id", line, f'id="{a["id"]}" also used on line {self.ids[a["id"]]}'))
            else:
                self.ids[a["id"]] = line
        if tag == "img" and "alt" not in a:
            self.issues.append(("html-img-no-alt", line, "<img> without alt attribute"))
        if tag == "a" and (a.get("target") == "_blank") and "noopener" not in (a.get("rel") or "") and "noreferrer" not in (a.get("rel") or ""):
            self.issues.append(("html-blank-no-noopener", line, '<a target="_blank"> without rel="noopener"'))
        if tag not in VOID:
            self.stack.append((tag, line))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID and self.stack and self.stack[-1][0] == tag:
            self.stack.pop()

    def handle_endtag(self, tag):
        line = self.getpos()[0]
        if tag in VOID:
            return
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                for t, l in self.stack[i + 1:]:
                    if t not in OPTIONAL_CLOSE:
                        self.issues.append(("html-unclosed-tag", l, f"<{t}> opened here is not closed before </{tag}> (line {line})"))
                del self.stack[i:]
                return
        self.issues.append(("html-stray-end-tag", line, f"</{tag}> has no matching start tag"))


class HtmlAnalyzer(BaseAnalyzer):
    name, languages = "html", ("HTML",)

    def is_available(self) -> bool:
        return True

    def analyze(self, files: Mapping[str, str]) -> ToolResult:
        res = ToolResult(analyzer="html", version="builtin html.parser", command="(in-process)", kind="heuristic")
        sev = {"html-unclosed-tag": "low", "html-stray-end-tag": "low", "html-duplicate-id": "low",
               "html-img-no-alt": "low", "html-blank-no-noopener": "low"}
        for path, text in files.items():
            if not path.lower().endswith((".html", ".htm")) or "{{" in text and "{%" in text:     # skip server templates
                continue
            p = _Html()
            try:
                p.feed(text)
                p.close()
            except Exception as exc:  # noqa: BLE001 - malformed markup must not stop analysis
                res.findings.append(_f("html-parse-error", "low", "syntax", "HTML could not be parsed", path, None, str(exc)[:200],
                                       "The HTML parser raised an error.", "Validate the markup.", "phase3:html"))
                continue
            for rule, line, ev in p.issues[:50]:
                res.findings.append(_f(rule, sev[rule], "html", rule.replace("html-", "HTML: ").replace("-", " "),
                                       path, line, ev, "Structural check (heuristic) by the built-in HTML parser; browsers may recover silently.",
                                       "Fix the markup; validate with the W3C validator.", "phase3:html"))
            if p.has_html and not p.has_lang:
                res.findings.append(_f("html-missing-lang", "info", "accessibility", "HTML: <html> has no lang attribute", path, 1,
                                       "<html> without lang", "Screen readers and search engines use lang.", 'Add lang="en" (or the right code).', "phase3:html"))
            if p.has_html and not p.has_title:
                res.findings.append(_f("html-missing-title", "info", "accessibility", "HTML: page has no <title>", path, 1, "no <title>",
                                       "Every page should have a title.", "Add a <title>.", "phase3:html"))
        res.status = "findings" if res.findings else "ok"
        return res


# ------------------------------------------------------------------------------------------------ CSS
class CssAnalyzer(BaseAnalyzer):
    name, languages = "css", ("CSS",)

    def is_available(self) -> bool:
        return True

    def analyze(self, files: Mapping[str, str]) -> ToolResult:
        res = ToolResult(analyzer="css", version="builtin", command="(in-process)", kind="heuristic")
        for path, text in files.items():
            if not path.lower().endswith((".css", ".scss")):
                continue
            stripped, i, n, comment_line = [], 0, len(text), None
            depth, line, opened = 0, 1, []
            in_str = ""
            while i < n:
                ch = text[i]
                if ch == "\n":
                    line += 1
                if in_str:
                    if ch == "\\":
                        i += 1
                    elif ch == in_str:
                        in_str = ""
                elif text.startswith("/*", i):
                    comment_line = line
                    end = text.find("*/", i + 2)
                    if end == -1:
                        res.findings.append(_f("css-unterminated-comment", "low", "syntax", "CSS: unterminated comment", path, line, "/* ...",
                                               "The comment never closes, so the rest of the file is ignored.", "Close it with */.", "phase3:css"))
                        break
                    line += text.count("\n", i, end)
                    i = end + 2
                    continue
                elif ch in "\"'":
                    in_str = ch
                elif ch == "{":
                    depth += 1
                    opened.append(line)
                elif ch == "}":
                    if depth == 0:
                        res.findings.append(_f("css-unmatched-brace", "low", "syntax", "CSS: unmatched closing brace", path, line, "}",
                                               "A } has no matching {.", "Remove it or add the missing {.", "phase3:css"))
                    else:
                        depth -= 1
                        opened.pop()
                i += 1
            if depth > 0 and not any(f.file == path and f.rule_id == "css-unterminated-comment" for f in res.findings):
                res.findings.append(_f("css-unclosed-brace", "low", "syntax", "CSS: block is never closed", path, opened[-1],
                                       "{", f"{depth} block(s) opened but not closed.", "Add the missing }.", "phase3:css"))
        res.status = "findings" if res.findings else "ok"
        return res


# ------------------------------------------------------------------------------------------------ PHP
_PHP_RULES = [
    (re.compile(r"\beval\s*\("), "php-eval", "high", "PHP: eval() executes arbitrary code", "Avoid eval(); use data formats or a safe dispatch table."),
    (re.compile(r"\bmysql_(?:query|connect|fetch_\w+|real_escape_string)\s*\("), "php-mysql-removed", "medium",
     "PHP: removed mysql_* extension", "Use PDO or mysqli with prepared statements."),
    (re.compile(r"\b(?:mysqli_query|->query)\s*\([^;]*\$_(?:GET|POST|REQUEST|COOKIE)"), "php-sqli-superglobal", "high",
     "PHP: query built from request data", "Use prepared statements with bound parameters."),
    (re.compile(r"\b(?:shell_exec|system|passthru|exec|popen|proc_open)\s*\([^;]*\$_(?:GET|POST|REQUEST)"), "php-command-injection", "high",
     "PHP: shell command built from request data", "Never pass request data to a shell; validate against an allow-list."),
    (re.compile(r"\b(?:include|require)(?:_once)?\s*\(?\s*\$_(?:GET|POST|REQUEST)"), "php-file-inclusion", "high",
     "PHP: include of a request-controlled path", "Map user input to a fixed allow-list of files."),
    (re.compile(r"\bunserialize\s*\(\s*\$_(?:GET|POST|REQUEST|COOKIE)"), "php-unserialize", "high",
     "PHP: unserialize() on request data", "Use json_decode() instead."),
    (re.compile(r"\becho\s+[^;]*\$_(?:GET|POST|REQUEST)"), "php-xss-echo", "medium", "PHP: request data echoed without escaping",
     "Wrap output in htmlspecialchars($v, ENT_QUOTES, 'UTF-8')."),
]


class PhpAnalyzer(BaseAnalyzer):
    name, languages = "php", ("PHP",)

    def is_available(self) -> bool:
        return shutil.which("php") is not None

    def version(self) -> str:
        try:
            return _run(["php", "-v"], timeout=10)[1].splitlines()[0][:80]
        except (OSError, subprocess.SubprocessError, IndexError):
            return ""

    def analyze(self, files: Mapping[str, str]) -> ToolResult:
        res = ToolResult(analyzer="php", kind="parser", command="php -l <file>")
        php = {p: t for p, t in files.items() if p.lower().endswith((".php", ".phtml"))}
        for path, text in php.items():
            for rx, rid, sev, title, fix in _PHP_RULES:
                for m in rx.finditer(text):
                    line = text.count("\n", 0, m.start()) + 1
                    res.findings.append(_f(rid, sev, "security", title, path, line, text.splitlines()[line - 1].strip(),
                                           "Pattern match (heuristic); data flow was not traced.", fix, "phase3:php", "low"))
        if not php:
            return res
        if not self.is_available():
            res.status, res.note = "unavailable", "PHP is not installed; syntax check skipped (install PHP and ensure `php` is on PATH)."
            return res
        res.version = self.version()
        tmp = Path(tempfile.mkdtemp(prefix="ase_php_", dir=_scratch_dir()))
        try:
            for i, (path, text) in enumerate(php.items()):
                f = tmp / f"f{i}.php"
                f.write_text(text, encoding="utf-8")
                try:
                    rc, out, err = _run(["php", "-d", "display_errors=1", "-l", str(f)], timeout=15)
                except subprocess.TimeoutExpired:
                    res.note += f"timeout on {path}; "
                    continue
                res.exit_code = rc if res.exit_code in (None, 0) else res.exit_code
                if rc != 0:
                    msg = (out + err).strip().replace(str(f), path)
                    m = re.search(r"on line (\d+)", msg)
                    res.findings.append(_f("php-syntax", "high", "syntax", "PHP syntax error (reported by php -l)", path,
                                           int(m.group(1)) if m else None, msg.splitlines()[0] if msg else "php -l failed",
                                           "php -l parsed the file and rejected it.", "Fix the syntax error at the reported line.",
                                           "phase3:php", "high", "tool-reported"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        res.status = "findings" if res.findings else "ok"
        return res


# ------------------------------------------------------------------------------------------------ Go
_GO_RULES = [
    (re.compile(r"InsecureSkipVerify\s*:\s*true"), "go-tls-skip-verify", "high", "Go: TLS certificate verification disabled",
     "Remove InsecureSkipVerify or pin the certificate."),
    (re.compile(r"\bexec\.Command\s*\([^)]*\+"), "go-command-concat", "medium", "Go: command built by concatenation",
     "Pass arguments separately and validate input."),
    (re.compile(r"\bmath/rand\b"), "go-weak-rand", "info", "Go: math/rand is not cryptographically secure", "Use crypto/rand for secrets and tokens."),
    (re.compile(r"\bfmt\.Sprintf\s*\(\s*\"[^\"]*(?:SELECT|INSERT|UPDATE|DELETE)[^\"]*%[sv]", re.I), "go-sql-sprintf", "high",
     "Go: SQL built with fmt.Sprintf", "Use placeholders (db.Query(q, args...))."),
]


class GoAnalyzer(BaseAnalyzer):
    name, languages = "go", ("Go",)

    def is_available(self) -> bool:
        return shutil.which("gofmt") is not None

    def version(self) -> str:
        try:
            return _run(["go", "version"], timeout=10)[1].strip()[:80]
        except (OSError, subprocess.SubprocessError):
            return ""

    def analyze(self, files: Mapping[str, str]) -> ToolResult:
        res = ToolResult(analyzer="go", kind="parser", command="gofmt -e (stdin)")
        gos = {p: t for p, t in files.items() if p.lower().endswith(".go")}
        for path, text in gos.items():
            for rx, rid, sev, title, fix in _GO_RULES:
                for m in rx.finditer(text):
                    line = text.count("\n", 0, m.start()) + 1
                    res.findings.append(_f(rid, sev, "security", title, path, line, text.splitlines()[line - 1].strip(),
                                           "Pattern match (heuristic).", fix, "phase3:go", "low"))
        if not gos:
            return res
        if not self.is_available():
            res.status, res.note = "unavailable", "Go is not installed; syntax check skipped (install Go from go.dev and ensure `gofmt` is on PATH)."
            return res
        res.version = self.version()
        for path, text in gos.items():
            try:
                rc, out, err = _run(["gofmt", "-e"], inp=text, timeout=15)
            except subprocess.TimeoutExpired:
                res.note += f"timeout on {path}; "
                continue
            if rc != 0:
                for ln in err.splitlines()[:1]:           # first error only: later ones are usually cascades
                    m = re.search(r":(\d+):(\d+): (.*)", ln)
                    if m:
                        res.findings.append(_f("go-syntax", "high", "syntax", "Go syntax error (reported by gofmt)", path, int(m.group(1)),
                                               m.group(3)[:200], "gofmt parsed the file and rejected it.", "Fix the syntax error.",
                                               "phase3:go", "high", "tool-reported"))
                res.exit_code = rc
        res.status = "findings" if res.findings else "ok"
        return res


BUILTIN_ANALYZERS = (HtmlAnalyzer, CssAnalyzer, PhpAnalyzer, GoAnalyzer)


def run_builtin_analyzers(texts: Mapping[str, str]) -> Tuple[List[Finding], List[dict]]:
    findings: List[Finding] = []
    runs: List[dict] = []
    for cls in BUILTIN_ANALYZERS:
        a = cls()
        present = any(_lang_match(a.name, p) for p in texts)
        if not present:
            continue
        try:
            r = a.analyze(texts)
        except Exception as exc:  # noqa: BLE001 - one analyzer failing must not hide the others
            runs.append({"analyzer": a.name, "status": "error", "version": "", "command": [], "exit_code": None,
                         "note": f"{type(exc).__name__}: {str(exc)[:150]}", "stdout": "", "stderr": ""})
            continue
        findings.extend(r.findings)
        runs.append({"analyzer": a.name, "status": r.status, "version": r.version, "command": [r.command] if r.command else [],
                     "exit_code": r.exit_code, "note": r.note, "stdout": r.stdout, "stderr": r.stderr, "kind": r.kind})
    return findings, runs


def _lang_match(name: str, path: str) -> bool:
    ext = {"html": (".html", ".htm"), "css": (".css", ".scss"), "php": (".php", ".phtml"), "go": (".go",)}[name]
    return path.lower().endswith(ext)
