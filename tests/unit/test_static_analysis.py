from core.analysis_orchestrator import analyze_archive
from core.static_analysis import check_python_syntax, scan_security, scan_todos


def rule_ids(findings):
    return {f.rule_id for f in findings}


def test_python_syntax_error_has_real_line_and_is_confirmed():
    f = check_python_syntax("a.py", "x = 1\n\ndef bad(:\n    pass\n")[0]
    assert f.rule_id == "py-syntax" and f.line == 3
    assert f.confidence == "confirmed" and f.verification == "verified-by-parser"


def test_valid_python_has_no_syntax_finding():
    assert check_python_syntax("a.py", "def ok():\n    return 1\n") == []


def test_python_null_byte_does_not_crash():
    assert check_python_syntax("a.py", "x = 1\x00")[0].rule_id in ("py-parse-failed", "py-syntax")


def test_todo_only_in_comments():
    src = "# TODO: a\nx = 'TODO not a comment'\n// FIXME b\nreturn 1 # HACK c\n"
    found = scan_todos("f.py", src)
    assert [f.line for f in found] == [1, 3, 4]


def test_security_rules_positive():
    src = ('eval(x)\nsubprocess.run(c, shell=True)\nrequests.get(u, verify=False)\n'
           'cur.execute(f"select {a}")\nhashlib.md5(b)\npickle.loads(d)\nPASSWORD = "abcd1234"\n')
    ids = rule_ids(scan_security("a.py", src))
    assert {"sec-eval-exec", "sec-shell-true", "sec-tls-verify-off", "sec-sql-format",
            "sec-weak-hash", "sec-pickle", "sec-hardcoded-secret"} <= ids


def test_security_rules_negative():
    src = ("# eval(x) in a comment\nmodel.eval()\nast.literal_eval(s)\nPASSWORD = os.environ['P']\n"
           "PASSWORD = 'changeme'\nsubprocess.run([c])\n")
    assert scan_security("a.py", src) == []


def test_language_specific_rule_not_applied_to_other_languages():
    assert "sec-pickle" not in rule_ids(scan_security("a.js", "pickle.loads(x)"))
    assert "sec-innerhtml" in rule_ids(scan_security("a.js", "el.innerHTML = x"))


def test_private_key_and_aws_key_detected_everywhere():
    ids = rule_ids(scan_security("k.txt", "-----BEGIN RSA PRIVATE KEY-----\nAKIAABCDEFGHIJKLMNOP\n"))
    assert {"sec-private-key", "sec-aws-key"} <= ids


def test_heuristics_not_labelled_confirmed():
    for f in scan_security("a.py", "eval(x)\n"):
        assert f.confidence != "confirmed" and f.verification == "unverified-heuristic"


def test_findings_sorted_numbered_and_capped(make_zip):
    lines = "".join(f"# TODO item {i}\n" for i in range(150))
    r = analyze_archive(make_zip({"a.py": lines + "def x(:\n"}))
    assert r.findings[0].rule_id == "py-syntax" and r.findings[0].id == "F-0001"
    assert sum(1 for f in r.findings if f.rule_id == "todo-marker") == 100
    assert any("matched 150 times" in w for w in r.warnings)
