"""Portfolio batch analysis: discovery, verdicts, similarity, safe output, CLI and UI smoke."""
import csv
import json
import threading
import time
import zipfile
from pathlib import Path

import pytest

from institution.portfolio import (
    PROFILES, ProjectRow, analyze_portfolio, discover_archives, evaluate, load_profiles, risk_score, similarity,
    to_csv_rows, to_html, write_portfolio_reports, _csv_safe,
)

CLEAN = {f"app/mod{i}.py": f"def add{i}(a, b):\n    return a + b + {i}\n" * 8 for i in range(5)}
BAD = {"app/main.py": "import os\n\ndef run(x):\n    return eval(x)\n" * 6,
       "app/config.py": 'API_KEY = "abcd1234efgh5678ijkl"\nPASSWORD = "Sup3rS3cret!!"\n',
       "app/broken.py": "def oops(:\n    return 1\n" * 4}


def mk(folder: Path, name: str, files: dict) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / name
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as zf:
        for n, c in files.items():
            zf.writestr(n, c)
    return p


@pytest.fixture
def batch(tmp_path):
    src = tmp_path / "subs"
    mk(src, "alice.zip", CLEAN)
    mk(src, "bob.zip", BAD)
    mk(src, "carol_copy.zip", {**CLEAN, "extra.py": "x = 1\n" * 30})     # near-duplicate of alice
    (src / "notes.txt").write_text("not a zip")
    (src / "broken.zip").write_bytes(b"this is not a zip file")
    return src


def test_discover_non_recursive_and_recursive(batch):
    mk(batch / "deep", "dave.zip", CLEAN)
    flat, _ = discover_archives(batch)
    assert [p.name for p in flat] == ["alice.zip", "bob.zip", "broken.zip", "carol_copy.zip"]
    deep, _ = discover_archives(batch, recursive=True)
    assert any(p.name == "dave.zip" for p in deep) and len(deep) == 5
    assert discover_archives(batch / "missing")[0] == []


def test_batch_survives_bad_archive_and_scores_projects(batch, tmp_path):
    res = analyze_portfolio(batch, tmp_path / "out", profile="enterprise")
    rows = {r.name: r for r in res.rows}
    assert set(rows) == {"alice.zip", "bob.zip", "broken.zip", "carol_copy.zip"}
    assert rows["broken.zip"].status == "rejected" and rows["broken.zip"].verdict == "FAIL"
    assert rows["alice.zip"].verdict == "PASS" and rows["alice.zip"].risk_score == 0
    bob = rows["bob.zip"]
    assert bob.status == "ok" and bob.verdict == "FAIL"
    assert bob.secrets >= 1 and bob.syntax_errors >= 1 and bob.risk_score > 0
    assert res.totals["analysed"] == 3 and res.totals["rejected"] == 1


def test_similarity_flags_copied_submission(batch, tmp_path):
    res = analyze_portfolio(batch, tmp_path / "out", profile="education")
    rows = {r.name: r for r in res.rows}
    assert rows["carol_copy.zip"].similar_to and rows["carol_copy.zip"].similar_to[0]["name"] == "alice.zip"
    assert rows["carol_copy.zip"].verdict == "REVIEW"
    assert rows["alice.zip"].similar_to[0]["name"] == "carol_copy.zip"
    assert rows["bob.zip"].similar_to == []


def test_identical_archives_are_100_percent(tmp_path):
    src = tmp_path / "s"
    a = mk(src, "a.zip", CLEAN)
    (src / "b.zip").write_bytes(a.read_bytes())
    res = analyze_portfolio(src, tmp_path / "o")
    assert res.rows[0].similar_to[0]["similarity"] == 1.0


def test_reports_written_and_linked(batch, tmp_path):
    out = tmp_path / "out"
    res = analyze_portfolio(batch, out)
    paths = write_portfolio_reports(res, out)
    assert all(p.is_file() for p in paths.values())
    data = json.loads(paths["json"].read_text(encoding="utf-8"))
    assert data["profile"] == "general" and len(data["rows"]) == 4
    alice = next(r for r in res.rows if r.name == "alice.zip")
    assert (out / alice.report_html).is_file() and (out / alice.report_json).is_file()
    with open(paths["csv"], encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.reader(fh))
    assert rows[0][0] == "project" and len(rows) == 5


def test_csv_and_html_neutralise_hostile_names():
    assert _csv_safe("=HYPERLINK(\"http://x\")").startswith("'")
    assert _csv_safe("+1+1").startswith("'") and _csv_safe("normal") == "normal"
    from institution.portfolio import PortfolioResult
    res = PortfolioResult(rows=[ProjectRow(name="<script>alert(1)</script>.zip", status="ok", verdict="PASS",
                                           reasons=["<img src=x onerror=1>"])], totals={"archives": 1})
    page = to_html(res)
    assert "<script>alert" not in page and "&lt;script&gt;" in page and "<img src=x" not in page
    assert to_csv_rows(PortfolioResult(rows=[ProjectRow(name="=cmd|calc")]))[0][0] == "'=cmd|calc"


def test_cancel_stops_cleanly(batch, tmp_path):
    ev = threading.Event()
    ev.set()
    res = analyze_portfolio(batch, tmp_path / "o", cancel=ev)
    assert res.cancelled and res.rows == []


def test_evaluate_is_pure_and_profile_dependent():
    row = ProjectRow(name="x", status="ok", severity_counts={"high": 4}, secrets=0)
    assert evaluate(row, PROFILES["general"])[0] == "PASS"
    assert evaluate(row, PROFILES["government"])[0] == "FAIL"
    assert evaluate(ProjectRow(name="y", status="error", message="boom"), PROFILES["general"])[0] == "n/a"
    assert risk_score({"critical": 4, "high": 1}) == 100 and risk_score({"medium": 2}) == 6


def test_similarity_math():
    a, b = frozenset({(1, 100), (2, 100)}), frozenset({(2, 100), (3, 100)})
    assert similarity(a, b) == pytest.approx(1 / 3)
    assert similarity(frozenset(), b) == 0.0


def test_unknown_profile_and_profile_file_validation(tmp_path):
    with pytest.raises(ValueError):
        analyze_portfolio(tmp_path, tmp_path / "o", profile="nope")
    f = tmp_path / "p.json"
    f.write_text(json.dumps({"profiles": {"strict": {"max_high": 1, "max_critical": "lots", "similarity_review_at": 5},
                                          "../bad": {"max_high": 1}}}))
    profiles, warns = load_profiles(f)
    assert profiles["strict"]["max_high"] == 1 and profiles["strict"]["max_critical"] == 0
    assert "../bad" not in profiles and len(warns) == 3
    assert load_profiles(tmp_path / "missing.json")[1] == []
    f.write_text("{not json")
    assert load_profiles(f)[0]["general"] and load_profiles(f)[1]


def test_shipped_profiles_file_matches_builtin_defaults():
    profiles, warns = load_profiles(Path(__file__).resolve().parents[2] / "config" / "institution_profiles.json")
    assert warns == [] and profiles == {k: dict(v) for k, v in PROFILES.items()}


def test_cli_batch_exit_codes(batch, tmp_path, capsys):
    import app
    assert app.run_batch(str(batch), str(tmp_path / "o1"), "general", False) == 1          # bob FAILs
    out = capsys.readouterr().out
    assert "Portfolio HTML" in out and "Portfolio CSV" in out
    assert app.run_batch(str(batch), str(tmp_path / "o2"), "nope", False) == 2
    empty = tmp_path / "empty"
    empty.mkdir()
    assert app.run_batch(str(empty), str(tmp_path / "o3"), "general", False) == 2


def test_source_zips_are_not_modified(batch, tmp_path):
    before = {p.name: p.read_bytes() for p in batch.iterdir()}
    analyze_portfolio(batch, tmp_path / "o")
    assert {p.name: p.read_bytes() for p in batch.iterdir()} == before
    assert sorted(p.name for p in batch.iterdir()) == sorted(before)       # nothing extracted or added next to the zips


# ------------------------------------------------------------------------------------------------ UI
tk = pytest.importorskip("tkinter")


def _root():
    try:
        return tk.Tk()
    except tk.TclError:
        pytest.skip("No display available (run under xvfb-run or on a desktop)")


def test_portfolio_tab_runs_end_to_end(batch, tmp_path):
    from ui.portfolio_view import PortfolioView
    root = _root()
    try:
        view = PortfolioView(root, tmp_path / "out", profiles_path=None)
        view.pack(fill="both", expand=True)
        view.folder_var.set(str(batch))
        view.start()
        end = time.time() + 60
        while view._worker.is_alive() or str(view.start_btn["state"]) == "disabled":
            root.update()
            time.sleep(0.01)
            assert time.time() < end, "portfolio worker did not finish"
        for _ in range(5):
            root.update()
        assert len(view.tree.get_children()) == 4
        assert str(view.open_html_btn["state"]) == "normal"
        verdicts = {view.tree.item(i, "values")[0]: view.tree.item(i, "values")[1] for i in view.tree.get_children()}
        assert verdicts["bob.zip"] == "FAIL" and verdicts["alice.zip"] == "REVIEW"      # alice/carol share 5 of 6 files
    finally:
        root.destroy()


def test_main_window_has_portfolio_tab(tmp_path):
    from core.settings import Settings
    from ui.main_window import MainWindow
    root = _root()
    try:
        mw = MainWindow(root, tmp_path / "reports", settings=Settings(history_enabled=False), base_dir=tmp_path)
        tabs = [mw.nb.tab(t, "text") for t in mw.nb.tabs()]
        assert "Portfolio" in tabs and "Overview" in tabs and "Settings" in tabs     # existing tabs intact
    finally:
        root.destroy()
