import pytest

from core.patch_generators import can_suggest, suggest_fix, why_no_fix
from core.static_analysis import analyze_text_file
from models.finding import Finding


def f(rule, line, file="a.py"):
    return Finding(rule_id=rule, severity="medium", category="security", title="t", file=file, line=line)


def first(text, rule, file="a.py"):
    found = [x for x in analyze_text_file(file, text) if x.rule_id == rule]
    assert found, f"{rule} not detected"
    return found[0]


def test_tls_fix_changes_only_that_line():
    src = "import requests\nr = requests.get(u, verify=False)\nq = 1\n"
    s = suggest_fix(first(src, "sec-tls-verify-off"), src)
    assert s.new_text == "import requests\nr = requests.get(u, verify=True)\nq = 1\n" and s.caution


def test_debug_and_yaml_fix():
    src = "DEBUG = True\ncfg = yaml.load(fh)\n"
    assert suggest_fix(first(src, "sec-debug-enabled"), src).new_text == "DEBUG = False\ncfg = yaml.load(fh)\n"
    assert "yaml.safe_load(fh)" in suggest_fix(first(src, "sec-pickle"), src).new_text


def test_pickle_has_no_automatic_fix():
    src = "obj = pickle.loads(data)\n"
    assert suggest_fix(first(src, "sec-pickle"), src) is None


def test_eval_fix_adds_import_after_docstring_and_future():
    src = '"""Doc."""\nfrom __future__ import annotations\n\nx = eval(s)\n'
    new = suggest_fix(first(src, "sec-eval-exec"), src).new_text
    assert new.splitlines()[:4] == ['"""Doc."""', "from __future__ import annotations", "import ast", ""]
    assert "ast.literal_eval(s)" in new and compile(new, "x", "exec")


def test_eval_import_not_duplicated():
    src = "import ast\nx = eval(s)\n"
    assert suggest_fix(first(src, "sec-eval-exec"), src).new_text.count("import ast") == 1


def test_secret_fix_uses_environment_and_keeps_comment_and_crlf():
    src = 'API_KEY = "abcd1234efgh"  # prod\r\nx = 1\r\n'
    s = suggest_fix(first(src, "sec-hardcoded-secret"), src)
    assert 'API_KEY = os.environ.get("API_KEY", "")  # prod\r\n' in s.new_text and s.new_text.startswith("import os\r\n")
    assert "abcd1234efgh" not in s.new_text and "rotate" in s.caution


def test_no_patch_when_pattern_not_on_reported_line():
    src = "a = 1\nb = 2\nDEBUG = True\n"
    assert suggest_fix(f("sec-debug-enabled", 1), src) is None          # wrong line -> nothing fabricated
    assert suggest_fix(f("sec-debug-enabled", 99), src) is None
    assert "reported line" in why_no_fix(f("sec-debug-enabled", 1), src)


def test_unsupported_cases():
    assert suggest_fix(f("py-syntax", 1), "def x(:\n") is None and "manual" in why_no_fix(f("py-syntax", 1))
    assert suggest_fix(f("sec-debug-enabled", 1, "a.js"), "DEBUG = True\n") is None
    assert not can_suggest(Finding("sec-debug-enabled", "low", "c", "t", file="a.py", line=None))
    assert "No automatic fix" in why_no_fix(f("sec-sql-format", 1))
    assert suggest_fix(f("sec-eval-exec", 1), "x = ast.literal_eval(s)\n") is None
    assert suggest_fix(f("sec-eval-exec", 1), "x = model.eval()\n") is None


def test_unparseable_file_gets_no_import_edit():
    assert suggest_fix(f("sec-eval-exec", 2), "def (:\nx = eval(s)\n") is None
