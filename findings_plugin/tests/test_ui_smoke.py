import pytest

tk = pytest.importorskip("tkinter", reason="tkinter not installed in this environment")


def _root():
    try:
        return tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"no display available: {exc}")


def test_panel_mounts_filters_and_threadsafe_update():
    import threading
    from tkinter import ttk
    from findings_plugin import register, Finding, Severity
    root = _root()
    try:
        nb = ttk.Notebook(root)
        nb.pack()
        plugin = register(nb, tab_text="Findings")
        assert nb.tab(0, "text") == "Findings"
        plugin.add([Finding("a", Severity.HIGH, file_path="x.py", line=2), Finding("b", Severity.LOW)])
        v = plugin.view
        v.refresh()
        assert {"grp:high", "grp:low"} <= set(v.tree.get_children())
        t = threading.Thread(target=lambda: plugin.add([Finding("from thread", Severity.CRITICAL)]))
        t.start(); t.join()
        for _ in range(5):
            root.update(); root.after(200)
        assert v.tree.exists("grp:critical")
        v.search_var.set("zzz"); v.refresh()
        assert not any(c.startswith("F-") for g in v.tree.get_children() for c in v.tree.get_children(g))
        v.clear_filters()
        first = plugin.store.query()[0]
        v.select(first.id)
        root.update()  # Tk delivers <<TreeviewSelect>> on the next event-loop pass
        assert "Summary" in v.detail.get("1.0", "end")
    finally:
        root.destroy()
