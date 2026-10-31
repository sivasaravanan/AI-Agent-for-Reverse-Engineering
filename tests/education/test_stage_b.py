import csv
import json
import random
import zipfile
from pathlib import Path

import pytest

from education import (RubricError, SimilarityOptions, compare_submissions, default_rubric, discover_submissions, grade_batch,
                       load_manual_scores, load_rubric)
from education.reports import csv_safe
from education.rubric import evaluate


def gen_code(seed: int, n: int = 12) -> str:
    """Distinct but realistic Python: n different functions."""
    rnd = random.Random(seed)
    out = []
    for i in range(n):
        ops = [rnd.choice("+-*") for _ in range(4)]
        out.append(f"def fn_{seed}_{i}(a, b):\n    \"\"\"doc\"\"\"\n    x = a {ops[0]} b\n    for k in range({rnd.randint(2, 9)}):\n"
                   f"        if x % {rnd.randint(2, 7)} == {rnd.randint(0, 1)}:\n            x = x {ops[1]} k {ops[2]} {rnd.randint(1, 50)}\n"
                   f"        else:\n            x = x {ops[3]} {rnd.randint(1, 50)}\n    return x\n")
    return "\n".join(out)


def make_zip(path: Path, files: dict) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for n, t in files.items():
            zf.writestr(n, t)
    return path


GOOD = {"README.md": "# hi", "requirements.txt": "flask\n", "tests/test_a.py": "def test_a():\n    assert True\n"}


def good(seed):
    return {**GOOD, "a.py": gen_code(seed), "b.py": gen_code(seed + 100, 4), "c.py": gen_code(seed + 200, 4)}


@pytest.fixture
def batch_dir(tmp_path):
    d = tmp_path / "subs"
    d.mkdir()
    make_zip(d / "alice.zip", good(1))
    src = good(2)
    make_zip(d / "bob.zip", src)
    # carol copies bob with renamed identifiers
    cp = {k: (v.replace("fn_", "calc_").replace(" a,", " p,").replace("x ", "y ")) if k.endswith(".py") and not k.startswith("tests") else v for k, v in src.items()}
    make_zip(d / "carol.zip", cp)
    make_zip(d / "dave.zip", {"main.py": "password = 'hunter2hunter2'\nprint(1)\n"})
    (d / "erin.zip").write_bytes(b"not a zip")
    return d


# ------------------------------------------------------------------ rubric
def test_rubric_validation_errors():
    bad = [{}, {"criteria": []}, {"criteria": [{"id": "a b", "points": 1, "check": {"type": "manual"}}]},
           {"criteria": [{"id": "a", "points": 1, "check": {"type": "nope"}}]},
           {"criteria": [{"id": "a", "points": -1, "check": {"type": "manual"}}]},
           {"criteria": [{"id": "a", "points": 1, "check": {"type": "manual"}}, {"id": "a", "points": 1, "check": {"type": "manual"}}]},
           {"criteria": [{"id": "a", "points": 1, "check": {"type": "contains_text", "regex": "("}}]},
           {"criteria": [{"id": "a", "points": 1, "check": {"type": "file_exists"}}]}]
    for b in bad:
        with pytest.raises(RubricError):
            load_rubric(b)
    with pytest.raises(RubricError):
        load_rubric("{not json")
    with pytest.raises(RubricError):
        load_rubric("/nonexistent/rubric.json")


def test_rubric_loads_from_file_and_totals(tmp_path):
    p = tmp_path / "r.json"
    p.write_text(json.dumps({"name": "X", "criteria": [{"id": "a", "title": "A", "points": 5, "check": {"type": "manual"}},
                                                      {"id": "b", "points": 15, "check": {"type": "contains_text", "text": "hello"}}]}))
    r = load_rubric(p)
    assert (r.total, r.auto_total, r.manual_total) == (20, 15, 5)


def test_checks_evaluate_against_evidence():
    r = load_rubric({"criteria": [
        {"id": "readme", "points": 5, "check": {"type": "file_exists", "patterns": ["README*"]}},
        {"id": "hi", "points": 5, "check": {"type": "contains_text", "regex": r"def\s+main", "files": ["*.py"]}},
        {"id": "sev", "points": 10, "check": {"type": "findings_max", "severities": ["high"], "max": 0, "per_extra_penalty": 4}},
        {"id": "py", "points": 2, "check": {"type": "language_present", "language": "Python"}}]})
    res = {"findings": [{"severity": "high"}, {"severity": "high"}], "languages": [{"language": "Python"}], "manifests": []}
    ev = {e.criterion: e for e in evaluate(r, res, ["README.md", "a.py"], {"a.py": "def main(): pass"})}
    assert ev["readme"].status == "pass" and ev["hi"].points == 5 and ev["py"].points == 2
    assert ev["sev"].points == 2 and ev["sev"].status == "partial"          # 10 - 4*2


# ------------------------------------------------------------------ similarity
def test_similarity_catches_renamed_copy_and_not_independent_work():
    a, b = gen_code(1), gen_code(2)
    copy = a.replace("fn_1", "calc").replace(" a,", " p,").replace("x ", "acc ")
    pairs, notes = compare_submissions({"a": {"m.py": a}, "b": {"m.py": b}, "c": {"m.py": copy}})
    top = pairs[0]
    assert {top.a, top.b} == {"a", "c"} and top.level == "high" and top.files
    assert not any({p.a, p.b} == {"a", "b"} and p.level != "low" for p in pairs)
    assert any("lead for human review" in n for n in notes)


def test_template_and_common_boilerplate_are_excluded():
    tpl = gen_code(9)
    subs = {s: {"m.py": tpl + gen_code(i)} for i, s in enumerate("abcde")}
    pairs, notes = compare_submissions(subs, template={"m.py": tpl})
    assert all(p.level == "low" for p in pairs) and any("template" in n for n in notes)
    pairs2, notes2 = compare_submissions(subs)        # without template, boilerplate shared by all is auto-detected
    assert all(p.level == "low" for p in pairs2) and any("boilerplate" in n for n in notes2)


def test_tiny_submissions_not_compared():
    pairs, notes = compare_submissions({"a": {"x.py": "print(1)"}, "b": {"x.py": "print(1)"}})
    assert pairs == [] and any("Too little" in n for n in notes)


def test_vendored_and_non_source_ignored():
    code = gen_code(3)
    pairs, _ = compare_submissions({"a": {"node_modules/x.js": code, "n.txt": code}, "b": {"node_modules/x.js": code, "n.txt": code}})
    assert pairs == []


# ------------------------------------------------------------------ batch
def test_discovery(tmp_path):
    d = tmp_path / "s"
    d.mkdir()
    make_zip(d / "Ann Lee.zip", {"a.py": "1"})
    make_zip(d / "Ann_Lee.zip", {"a.py": "1"})
    (d / "folder1").mkdir()
    (d / "notes.txt").write_text("x")
    (d / "x.exe").write_bytes(b"1")
    (d / ".hidden.zip").write_bytes(b"1")
    subs, notes = discover_submissions(d)
    ids = [s for s, _ in subs]
    assert ids == ["Ann_Lee", "Ann_Lee_2", "folder1"] and any("Duplicate" in n for n in notes) and any("x.exe" in n for n in notes)
    with pytest.raises(Exception):
        discover_submissions(tmp_path / "missing")


def test_batch_end_to_end(batch_dir, tmp_path):
    out = tmp_path / "out"
    seen = []
    b = grade_batch(batch_dir, out_dir=out, progress=lambda f, m: seen.append(m))
    ids = {s.student_id: s for s in b.students}
    assert set(ids) == {"alice", "bob", "carol", "dave", "erin"} and seen
    assert ids["erin"].status == "error" and "not a valid ZIP" in ids["erin"].error          # one bad file does not stop the batch
    a = ids["alice"]
    assert a.status == "graded" and a.auto_points >= 60 and not a.complete and a.manual_count == 1
    assert ids["dave"].auto_points < a.auto_points                                            # secret + missing README/tests
    assert any(c["id"] == "secrets" and c["points"] == 0 for c in ids["dave"].evaluations)
    assert ids["bob"].similarity_level == "high" and "carol" in ids["bob"].similar_to and ids["alice"].similarity_level == ""
    for name in ("summary.html", "grades.csv", "similarity.csv", "grades.json", "manual_scores_template.csv"):
        assert (out / name).exists(), name
    assert (out / "students" / "alice.html").exists() and (out / "students" / "erin.html").exists()


def test_student_report_hides_other_students_and_similarity(batch_dir, tmp_path):
    out = tmp_path / "out"
    grade_batch(batch_dir, out_dir=out)
    html = (out / "students" / "bob.html").read_text(encoding="utf-8")
    assert "carol" not in html and "imilar" not in html and "code was not run" in html
    assert "carol" in (out / "summary.html").read_text(encoding="utf-8")
    assert "hunter2hunter2" not in (out / "students" / "dave.html").read_text(encoding="utf-8")       # redacted


def test_manual_scores_complete_the_grade(batch_dir, tmp_path):
    m = tmp_path / "m.csv"
    m.write_text("student_id,criterion_id,points,comment\nalice,correct,99,great\nghost,correct,1,\n")
    b = grade_batch(batch_dir, manual_scores=m)
    a = b.by_id("alice")
    assert a.complete and a.manual_points == 15 and a.earned == a.auto_points + 15            # clamped to the criterion max
    assert a.percent is not None and any("ignored" in n and "ghost" in n for n in b.notes)
    with pytest.raises(ValueError):
        bad = tmp_path / "bad.csv"; bad.write_text("student_id,criterion_id,points\na,b,xyz\n"); load_manual_scores(bad)


def test_csv_export_is_injection_safe_and_parses(tmp_path):
    d = tmp_path / "s"
    d.mkdir()
    make_zip(d / "=cmd.zip", good(5))
    out = tmp_path / "o"
    grade_batch(d, out_dir=out)
    rows = list(csv.DictReader(open(out / "grades.csv", encoding="utf-8-sig")))
    assert rows[0]["student_id"] == "cmd" and rows[0]["status"] == "graded"
    assert csv_safe("=1+1") == "'=1+1" and csv_safe("@x") == "'@x" and csv_safe(5) == 5 and csv_safe("ok") == "ok"


def test_cancel_and_empty(tmp_path, batch_dir):
    import threading
    ev = threading.Event(); ev.set()
    b = grade_batch(batch_dir, cancel=ev)
    assert b.students == [] and any("Cancelled" in n for n in b.notes)
    empty = tmp_path / "e"; empty.mkdir()
    with pytest.raises(Exception, match="No submissions"):
        grade_batch(empty)


def test_folder_and_tar_submissions(tmp_path):
    import tarfile, io
    d = tmp_path / "s"
    d.mkdir()
    f = d / "folderstudent"; f.mkdir(); (f / "a.py").write_text(gen_code(1)); (f / "README.md").write_text("x")
    with tarfile.open(d / "tarstudent.tar.gz", "w:gz") as tf:
        data = gen_code(2).encode(); ti = tarfile.TarInfo("a.py"); ti.size = len(data); tf.addfile(ti, io.BytesIO(data))
    b = grade_batch(d)
    assert {s.student_id: s.status for s in b.students} == {"folderstudent": "graded", "tarstudent": "graded"}


def test_custom_rubric_used(batch_dir, tmp_path):
    r = load_rubric({"name": "Tiny", "criteria": [{"id": "r", "points": 10, "check": {"type": "file_exists", "patterns": ["README*"]}}]})
    b = grade_batch(batch_dir, rubric=r)
    assert b.by_id("alice").earned == 10 and b.by_id("alice").complete and b.by_id("dave").earned == 0


def test_cli_grade(batch_dir, tmp_path, capsys):
    import app
    rc = app.main(["--grade", str(batch_dir), "--out", str(tmp_path / "o")])
    out = capsys.readouterr().out
    assert rc == 0 and "Graded: 4/5" in out and (tmp_path / "o" / "grading" / "grades.csv").exists()
    assert app.main(["--grade", str(batch_dir), "--rubric", str(tmp_path / "nope.json"), "--out", str(tmp_path / "o")]) == 2
    assert app.main(["--grade", str(tmp_path / "nodir"), "--out", str(tmp_path / "o")]) == 2


def test_ui_grading_tab(batch_dir, tmp_path):
    import time
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("No display available")
    from ui.main_window import MainWindow
    try:
        win = MainWindow(root, tmp_path / "reports")
        gp = win.grading_panel
        gp.folder_var.set(str(batch_dir))
        gp.start()
        deadline = time.time() + 120
        while gp.batch is None:
            root.update(); time.sleep(0.02)
            assert time.time() < deadline
        for _ in range(3):
            root.update()
        rows = [gp.tree.item(i, "values") for i in gp.tree.get_children()]
        assert {r[0] for r in rows} == {"alice", "bob", "carol", "dave", "erin"} and str(gp.open_btn["state"]) == "normal"
    finally:
        root.destroy()
