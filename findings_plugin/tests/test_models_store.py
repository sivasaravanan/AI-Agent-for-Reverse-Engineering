import pytest
from findings_plugin import (Finding, FindingStore, Severity, Kind, Confidence, Verification, make_ai_finding)


def F(title="t", **kw):
    return Finding(title, **kw)


def test_line_never_invented_and_validated():
    f = F(file_path="a/b.py")
    assert f.line is None and f.location == "a/b.py"
    with pytest.raises(ValueError):
        F(line=0)
    with pytest.raises(ValueError):
        F(column=3)  # column without line


def test_backslash_paths_normalised_and_id_stable():
    a, b = F("x", file_path="a\\b.py", line=3), F("x", file_path="a/b.py", line=3)
    assert a.file_path == "a/b.py" and a.id == b.id and a.id.startswith("F-")


def test_ai_hypothesis_cannot_claim_certainty():
    with pytest.raises(ValueError):
        F(kind=Kind.AI_HYPOTHESIS, confidence=Confidence.CONFIRMED)
    with pytest.raises(ValueError):
        F(kind=Kind.AI_HYPOTHESIS, verification=Verification.VERIFIED)
    ai = make_ai_finding("maybe")
    assert ai.confidence is Confidence.WEAK and not ai.is_detected


def test_dedup_group_filter_search():
    s = FindingStore()
    items = [F("crit", severity=Severity.CRITICAL, category="security", file_path="x/a.py", line=1, language="python"),
             F("med sql", severity="medium", category="database", file_path="x/b.sql", line=5, language="sql", evidence="SELECT *"),
             F("low", severity=Severity.LOW, category="security", file_path="y/c.js", language="javascript")]
    assert s.add_many(items) == 3 and s.add_many(items) == 0 and not s.add(items[0])
    g = s.group_by_severity()
    assert list(g)[0] is Severity.CRITICAL and len(g) == 3
    assert [f.title for f in s.query(min_severity=Severity.MEDIUM)] == ["crit", "med sql"]
    assert [f.title for f in s.query(categories=["security"], languages=["javascript"])] == ["low"]
    assert [f.title for f in s.query(files=["X/B.SQL"])] == ["med sql"]
    assert [f.title for f in s.search("select")] == ["med sql"]
    assert s.counts()["critical"] == 1


def test_related_reasons():
    s = FindingStore()
    a = F("a", file_path="m.py", line=10, rule_id="R1", category="c")
    b = F("b", file_path="m.py", line=15, category="c")
    c = F("c", file_path="n.py", line=1, rule_id="R1", category="c")
    d = F("d", file_path="z.py", trace_id="T", category="c")
    e = F("e", file_path="q.py", trace_id="T", category="c")
    s.add_many([a, b, c, d, e])
    rel = {f.title: why for f, why in s.related(a.id)}
    assert rel == {"b": "same file, within 10 lines", "c": "same rule elsewhere"}
    assert s.related(d.id)[0][1] == "same exception chain"
    ai = make_ai_finding("why", related_to=a.id)
    s.add(ai)
    assert any("linked" in why for _, why in s.related(a.id))


def test_verification_requires_evidence_and_notifies():
    s, hits = FindingStore(), []
    s.subscribe(lambda: hits.append(1))
    f = F("x")
    s.add(f)
    with pytest.raises(ValueError):
        s.set_verification(f.id, Verification.VERIFIED, "  ")
    s.set_verification(f.id, Verification.VERIFIED, "pytest tests/ -> 12 passed")
    assert f.verification is Verification.VERIFIED and len(hits) == 2


def test_listener_failure_does_not_break_store():
    s = FindingStore()
    s.subscribe(lambda: 1 / 0)
    assert s.add(F("x"))


def test_json_roundtrip(tmp_path):
    s = FindingStore()
    s.add_many([F("a", line=2, file_path="a.py", extra={"k": 1}), make_ai_finding("h")])
    p = tmp_path / "f.json"
    s.save(p)
    s2 = FindingStore.load(p)
    assert sorted(f.id for f in s2) == sorted(f.id for f in s)
    with pytest.raises(ValueError):
        FindingStore.from_json('{"schema": "other"}')
