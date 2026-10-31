"""Fast, tolerant project copy for container runs (standard library only). New module, additive.

``sandbox.prepare_workspace`` asks ``prepare()`` first. The classic code copied the project file by file and only then found out it was
over the limit (a 567 MB folder cost ~24 s before "Refused"). This module looks first and copies once:

  1. FAST SCAN     sizes only (no copying), virtualenvs / .git / node_modules / symlinks skipped as before.
  2. SCRIPT RUN    ``python script.py`` in a project bigger than "Narrow above (MB)" (or over the limit): only the script, the project
                   modules it imports (transitively) and small data / config files next to it are copied - seconds, not minutes.
                   A data file bigger than "Largest data file (MB)" is left out and named in the notes; the run is not refused for it.
  3. OTHER COMMAND over the limit: refused as before, unless "Trim oversized projects" is on (or Quick run is used): big files and then
                   the biggest folders are left out until it fits, and the notes name exactly what was left out.
  4. A single FILE (not a ZIP) chosen as the project: that script and what it imports from its own folder.

  5. LEAN COPY (default, ``lean_copy``): a script run NEVER copies the whole folder / archive, whatever its size. It copies the script,
                   the project modules it imports (transitively), small data / config next to it, the project files its code names
                   (string literals such as "config.json" or "templates"), and what the user included by hand (``add_includes``).
                   Anything else is fetched on demand: the program reports a missing file, the panel offers to copy exactly that one.

Only copies are made; the project itself is only read. Whatever is left out is listed in the returned notes, so a later
"file not found" in the program output is explained, never mysterious.
"""
from __future__ import annotations

import ast
import contextvars
import difflib
import os
import re
import shutil
import stat
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Dict, List, Optional

from . import storage as _storage

MB = 1024 * 1024
QUICK: "contextvars.ContextVar[bool]" = contextvars.ContextVar("be_quick_prep", default=False)   # set by Quick run: always trim, never refuse
MAX_ENTRIES = 400_000
REF_DIR_MAX_FILES, REF_DIR_MAX_BYTES = 300, 20 * MB      # a folder named in the code is copied whole only when it is this small
SECRET_FILES = {".env", "id_rsa", "id_dsa", "id_ed25519"}
SECRET_SUFFIX = (".pem", ".key", ".pfx", ".p12")

# What the user asked to copy by hand (or accepted when the program reported a missing file), per project, for this session.
_INCLUDES: Dict[str, List[str]] = {}


def _key(source) -> str:
    try:
        return os.path.normcase(os.path.abspath(str(source)))
    except (OSError, ValueError):
        return str(source)


def includes_for(source) -> List[str]:
    return list(_INCLUDES.get(_key(source), []))


def add_includes(source, rels) -> List[str]:
    """Remember project-relative files / folders to copy into every run of ``source``. Returns the current list."""
    cur = _INCLUDES.setdefault(_key(source), [])
    for r in rels:
        r = str(r).replace("\\", "/").strip().lstrip("/")
        if r and ".." not in r.split("/") and "\x00" not in r and r not in cur:
            cur.append(r)
    return list(cur)


def clear_includes(source=None) -> None:
    if source is None:
        _INCLUDES.clear()
    else:
        _INCLUDES.pop(_key(source), None)


class _Unreadable(Exception):
    """The source cannot be read as a folder / ZIP / file: the classic code path reports it in its own words."""


@dataclass
class Entry:
    rel: str
    size: int
    path: Optional[Path] = None
    info: Optional[zipfile.ZipInfo] = None
    mode: int = 0


def _skip_names() -> set:
    from .discovery import SKIP_DIRS
    from .sandbox import EXTRA_SKIP
    return (SKIP_DIRS - {"bin", "obj", "target", "dist", "build"}) | set(EXTRA_SKIP)


def _reject(msg: str):
    from .sandbox import WorkspaceRejected
    return WorkspaceRejected(msg)


class Src:
    """Read-only view of the project: a folder, a ZIP or one file. ``entries`` lists what could be copied (junk already left out)."""

    def __init__(self, source, max_depth: int = 0):
        self.path = Path(source)
        self.kind = ""
        self.entries: List[Entry] = []
        self.notes: List[str] = []
        self._zip: Optional[zipfile.ZipFile] = None
        self.root = self.path
        self.single = ""                                   # for a single chosen file: its name
        if self.path.is_dir():
            self.kind = "dir"
            self._scan_dir(self.path, max_depth)
        elif self.path.is_file() and zipfile.is_zipfile(self.path):
            self.kind = "zip"
            self._scan_zip()
        elif self.path.is_file():
            self.kind = "file"
            self.single = self.path.name
            self.root = self.path.parent
            self._scan_dir(self.root, 3)
            if self.single not in {e.rel for e in self.entries}:
                st = self.path.stat()
                self.entries.append(Entry(self.single, st.st_size, self.path, None, st.st_mode))
        else:
            raise _Unreadable(str(source))
        self.total = sum(e.size for e in self.entries)

    # ---------------------------------------------------------------- scanning
    def _scan_dir(self, root: Path, max_depth: int) -> None:
        skip = _skip_names()
        stack = [(root, 0)]
        while stack:
            d, depth = stack.pop()
            try:
                kids = list(os.scandir(d))
            except OSError:
                continue
            for e in kids:
                try:
                    if e.is_symlink():
                        self.notes.append(f"skipped symlink: {Path(e.path).relative_to(root).as_posix()}")
                        continue
                    if e.is_dir(follow_symlinks=False):
                        if e.name in skip or os.path.isfile(os.path.join(e.path, "pyvenv.cfg")):
                            continue
                        if not max_depth or depth + 1 <= max_depth:
                            stack.append((Path(e.path), depth + 1))
                        continue
                    st = e.stat(follow_symlinks=False)
                    self.entries.append(Entry(Path(e.path).relative_to(root).as_posix(), st.st_size, Path(e.path), None, st.st_mode))
                except OSError:
                    continue
            if len(self.entries) > MAX_ENTRIES:
                raise _reject(f"The project has more than {MAX_ENTRIES:,} files; choose a smaller folder.")

    def _scan_zip(self) -> None:
        try:
            self._zip = zipfile.ZipFile(self.path)
        except (zipfile.BadZipFile, OSError) as exc:
            raise _Unreadable(str(exc)) from exc
        infos = [i for i in self._zip.infolist() if not i.is_dir()]
        if len(infos) > MAX_ENTRIES:
            raise _reject("Archive has too many entries.")
        names = [i.filename.replace("\\", "/") for i in self._zip.infolist()]
        for n, i in zip(names, self._zip.infolist()):
            parts = PurePosixPath(n).parts
            if "\x00" in i.filename or n.startswith("/") or re.match(r"^[A-Za-z]:", n) or ".." in parts:
                raise _reject(f"Unsafe path in archive: {i.filename!r}")
        fnames = [i.filename.replace("\\", "/") for i in infos]
        tops = {PurePosixPath(n).parts[0] for n in fnames}
        strip = 1 if len(tops) == 1 and all(len(PurePosixPath(n).parts) > 1 for n in fnames) else 0
        skip = _skip_names()
        for i, n in zip(infos, fnames):
            parts = PurePosixPath(n).parts[strip:]
            mode = i.external_attr >> 16
            if not parts:
                continue
            if mode and stat.S_ISLNK(mode):
                self.notes.append(f"skipped symlink: {n}")
                continue
            if i.flag_bits & 1:
                self.notes.append(f"skipped encrypted member: {n}")
                continue
            if set(parts[:-1]) & skip:
                continue
            self.entries.append(Entry("/".join(parts), i.file_size, None, i, mode))

    # ---------------------------------------------------------------- reading / copying
    def read_text(self, e: Entry, limit: int = 2_000_000) -> str:
        try:
            if e.size > limit:
                return ""
            if e.path is not None:
                return e.path.read_text(encoding="utf-8", errors="replace")
            with self._zip.open(e.info) as fh:             # type: ignore[union-attr]
                return fh.read(limit + 1).decode("utf-8", "replace")
        except (OSError, KeyError, zipfile.BadZipFile):
            return ""

    def copy(self, e: Entry, dest: Path, budget_left: int) -> int:
        """Copy one entry into ``dest`` (project-relative path kept). Returns the bytes written; OSError means 'skip this file'."""
        out = dest.joinpath(*e.rel.split("/"))
        out.resolve().relative_to(dest.resolve())          # raises ValueError if it escaped
        out.parent.mkdir(parents=True, exist_ok=True)
        if e.path is not None:
            shutil.copyfile(e.path, out)
            n = e.size
        else:
            if e.info.file_size > MB and e.info.compress_size and e.info.file_size / e.info.compress_size > 200:
                raise _reject(f"Suspicious compression ratio: {e.info.filename!r}")
            n = 0
            with self._zip.open(e.info) as s_, open(out, "wb") as o_:   # type: ignore[union-attr]
                for chunk in iter(lambda: s_.read(65536), b""):
                    n += len(chunk)
                    if n > budget_left:
                        raise _reject("Archive expands beyond its declared size (zip bomb?).")
                    o_.write(chunk)
        try:
            os.chmod(out, 0o777 if (e.mode & stat.S_IXUSR) else 0o666)     # the container user is uid 65534
        except OSError:
            pass
        return n

    def close(self) -> None:
        if self._zip is not None:
            self._zip.close()


def _open_dirs(dest: Path) -> None:
    for q in [dest, *[x for x in dest.rglob("*") if x.is_dir()]]:
        try:
            os.chmod(q, 0o777)
        except OSError:
            pass


def _mb(n: float) -> str:
    return f"{n / MB:.0f} MB" if n >= MB else f"{n / 1024:.0f} KB"


def _biggest(src: Src, n: int = 3) -> str:
    sizes: Dict[str, int] = {}
    for e in src.entries:
        top = e.rel.split("/", 1)[0]
        sizes[top] = sizes.get(top, 0) + e.size
    return ", ".join(f"{k} ({v // MB} MB)" for k, v in sorted(sizes.items(), key=lambda kv: -kv[1])[:n])


def nearest_script(names: List[str], entry: str) -> str:
    """The one project script whose name is a close match for ``entry`` (a typo like dupescopy.py -> dupescope.py), else ''."""
    pys = [n for n in names if n.lower().endswith(".py") and "/" not in n]
    m = difflib.get_close_matches(entry, pys, n=2, cutoff=0.8)
    return m[0] if len(m) == 1 or (len(m) == 2 and difflib.SequenceMatcher(None, entry, m[0]).ratio() - difflib.SequenceMatcher(None, entry, m[1]).ratio() > 0.1) else ""


def resolve_entry(rels: Dict[str, Entry], entry: str) -> str:
    """The project path of the script (exact, then ignoring case). Raises a readable refusal with 'did you mean' when it is not there."""
    if entry in rels:
        return entry
    low = {k.lower(): k for k in rels}
    if entry.lower() in low:
        return low[entry.lower()]
    near = difflib.get_close_matches(entry, [k for k in rels if k.lower().endswith(".py") and "/" not in k], n=3, cutoff=0.6)
    hint = f" Did you mean {', '.join(near)}?" if near else ""
    raise _reject(f"'{entry}' was not found in the project, so there is nothing smaller to copy.{hint}")



def _is_secret(rel: str) -> bool:
    n = PurePosixPath(rel).name.lower()
    return n in SECRET_FILES or n.endswith(SECRET_SUFFIX) or n.startswith(".env")


def _dirs_of(rels) -> set:
    out = set()
    for r in rels:
        p = PurePosixPath(r).parent
        while str(p) not in (".", ""):
            out.add(str(p))
            p = p.parent
    return out


def referenced_files(src: "Src", rels: Dict[str, "Entry"], code_files, cap: int) -> tuple:
    """Project files / small folders named by string literals in the given code files ("config.json", "assets/logo.png", "templates").
    Returns (set of rels, notes). Reads only; strings that are not paths into this project are ignored; secrets are never taken."""
    dirs = _dirs_of(rels)
    found: set = set()
    notes: List[str] = []
    seen: set = set()
    for cf in sorted(code_files)[:NARROW_FILES]:
        try:
            tree = ast.parse(src.read_text(rels[cf]))
        except (SyntaxError, ValueError, MemoryError, KeyError):
            continue
        base = str(PurePosixPath(cf).parent)
        base = "" if base == "." else base
        for n in ast.walk(tree):
            if not (isinstance(n, ast.Constant) and isinstance(n.value, str)):
                continue
            v = n.value.strip().replace("\\", "/")
            if not (1 < len(v) <= 200) or "\n" in v or "://" in v or v.startswith(("/", "~", "<")) or ".." in v.split("/"):
                continue
            v = v[2:] if v.startswith("./") else v
            for cand in dict.fromkeys([v, f"{base}/{v}" if base else v]):
                if (cf, cand) in seen:
                    continue
                seen.add((cf, cand))
                if cand in rels:
                    if not _is_secret(cand) and (rels[cand].size <= cap or cand.lower().endswith(".py")):
                        found.add(cand)
                    break
                if cand in dirs:
                    under = [r for r in rels if r.startswith(cand + "/") and not _is_secret(r)]
                    size = sum(rels[r].size for r in under)
                    if under and len(under) <= REF_DIR_MAX_FILES and size <= REF_DIR_MAX_BYTES:
                        found.update(under)
                    elif under:
                        notes.append(f"{cand}/ ({len(under)} files, {_mb(size)}) is named in the code but too big to copy automatically - use Include... if the program needs it")
                    break
    return found, notes


def expand_includes(src: "Src", rels: Dict[str, "Entry"], items: List[str]) -> tuple:
    """The user's own includes (files or folders, project-relative) -> (set of rels, notes about items that were not found)."""
    out: set = set()
    notes: List[str] = []
    for it in items:
        it = it.strip("/")
        if it in rels:
            out.add(it)
        else:
            under = [r for r in rels if r.startswith(it + "/")]
            if under:
                out.update(under[:5000])
            else:
                notes.append(f"included '{it}' is not in the project")
    return out, notes


NARROW_FILES = 800

# ---------------------------------------------------------------------------------------------------------------- strategies
def _narrow(src: Src, dest: Path, entry: str, budget: int, cap: int, narrow_at: int = 0, lean: bool = False) -> List[str]:
    from .sandbox import python_closure
    rels = {e.rel: e for e in src.entries}
    try:
        entry = resolve_entry(rels, entry)
    except Exception as exc:                                # the size explanation stays in the message (what is big, and what was asked for)
        if src.total > budget:
            raise _reject(f"Project is larger than {budget // MB} MB (largest so far: {_biggest(src)}). {exc}") from exc
        raise
    if rels[entry].size > budget:
        raise _reject(f"'{entry}' is itself larger than {_mb(budget)}; nothing smaller can be copied.")
    reader = lambda r: src.read_text(rels[r]) if r in rels else ""                 # noqa: E731
    keep = python_closure(entry, set(rels), reader)
    keep.add(entry)
    extra_notes: List[str] = []
    forced: set = set()
    if lean:
        refs, rn = referenced_files(src, rels, {k for k in keep if k.lower().endswith(".py")}, cap)
        manual, mn = expand_includes(src, rels, includes_for(src.path))
        extra_notes += rn + mn
        forced = set(manual)
        for k in sorted(refs | manual):
            keep.add(k)
            if k.lower().endswith(".py"):                                          # a program it starts / loads: its own imports come too
                keep |= python_closure(k, set(rels), reader)
    code = {k for k in keep if k.lower().endswith((".py", ".pyw", ".pyi"))}
    left_out: List[str] = []
    chosen: List[str] = []
    for k in sorted(keep):
        e = rels[k]
        if k != entry and k not in code and k not in forced and e.size > cap:
            left_out.append(f"{k} ({_mb(e.size)})")
            continue
        chosen.append(k)
    total = sum(rels[k].size for k in chosen)
    if total > budget:                                      # drop data first (biggest first), code last
        for k in sorted((k for k in chosen if k != entry), key=lambda k: (k in code, -rels[k].size)):
            if total <= budget:
                break
            total -= rels[k].size
            chosen.remove(k)
            left_out.append(f"{k} ({_mb(rels[k].size)})")
    dest.mkdir(parents=True, exist_ok=True)
    written, skipped = 0, 0
    for k in chosen:
        try:
            written += src.copy(rels[k], dest, max(budget - written, 1))
        except OSError:
            skipped += 1
    _open_dirs(dest)
    if not (dest / Path(*entry.split("/"))).is_file():
        raise _reject(f"'{entry}' could not be copied (unreadable?).")
    lean_tail: List[str] = []
    over = src.total > budget or bool(narrow_at and src.total > narrow_at)      # the earlier reasons for a narrow copy still apply: keep their wording first
    if lean:
        extra = len(chosen) - 1
        joined = ", ".join(k for k in sorted(chosen) if k != entry)
        first = (f"Lean copy: only {entry} and {extra} other file(s) it needs ({_mb(written)} of the project's {_mb(src.total)}) were copied into the "
                 "container; the folder / archive itself was not copied and your files were not changed.")
        if extra:
            lean_tail.append("Copied with it: " + joined[:600] + ("..." if len(joined) > 600 else ""))
        lean_tail.append("If the program needs another file, use Include... (or accept the offer after it reports the file as missing).")
        lean_tail += extra_notes
        if not over:
            notes = [first] + lean_tail
            if left_out:
                notes.append("Left out of the copy (over the data-file limit): " + ", ".join(left_out[:8]) + ". Raise 'Largest data file (MB)' in Storage & clean-up, or Include... them.")
            if skipped:
                notes.append(f"{skipped} file(s) could not be read and were skipped.")
            return notes
    if src.total > budget or not narrow_at:             # over the project limit (the old wording stays for that case)
        why = f"Project is larger than {_mb(budget)} ({_mb(src.total)})"
    else:                                                # under the limit: the 'narrow above' setting made the quick copy, say so
        why = f"Project is larger than the quick-copy size of {_mb(narrow_at)} ({_mb(src.total)}; the limit is {_mb(budget)})"
    notes = [f"{why}, so only {entry} and {len(chosen) - 1} related file(s) (its local imports "
             "and small data / config files next to it) were copied into the container - a quick copy; your project was not changed."]
    if left_out:
        notes.append("Left out of the copy (too large for the project limit or the data-file limit): " + ", ".join(left_out[:8])
                     + (f" and {len(left_out) - 8} more" if len(left_out) > 8 else "")
                     + ". If the program needs one, raise 'Largest data file (MB)' in Storage & clean-up.")
    if skipped:
        notes.append(f"{skipped} file(s) could not be read and were skipped.")
    return notes + lean_tail


def _trim(src: Src, dest: Path, budget: int, cap: int) -> List[str]:
    ents = list(src.entries)
    left_out: List[str] = []
    big = [e for e in ents if e.size > cap]
    for e in sorted(big, key=lambda e: -e.size):
        left_out.append(f"{e.rel} ({_mb(e.size)})")
    ents = [e for e in ents if e.size <= cap]
    total = sum(e.size for e in ents)
    if total > budget:                                      # whole top-level folders, biggest first
        groups: Dict[str, int] = {}
        for e in ents:
            if "/" in e.rel:
                groups[e.rel.split("/", 1)[0]] = groups.get(e.rel.split("/", 1)[0], 0) + e.size
        for g, sz in sorted(groups.items(), key=lambda kv: -kv[1]):
            if total <= budget:
                break
            ents = [e for e in ents if not e.rel.startswith(g + "/")]
            total -= sz
            left_out.append(f"folder {g} ({_mb(sz)})")
    if total > budget:                                      # still too much: the biggest remaining files
        for e in sorted(ents, key=lambda e: -e.size):
            if total <= budget:
                break
            ents.remove(e)
            total -= e.size
            left_out.append(f"{e.rel} ({_mb(e.size)})")
    dest.mkdir(parents=True, exist_ok=True)
    written, skipped = 0, 0
    for e in ents:
        try:
            written += src.copy(e, dest, max(budget - written + MB, 1))
        except OSError:
            skipped += 1
    _open_dirs(dest)
    notes = [f"Project is larger than {_mb(budget)} ({_mb(src.total)}), so a trimmed copy was made ({len(ents)} file(s), {_mb(written)}); "
             "your project was not changed."]
    notes.append("Left out: " + ", ".join(left_out[:10]) + (f" and {len(left_out) - 10} more" if len(left_out) > 10 else "")
                 + ". Raise 'Largest project copied in' / 'Largest data file' in Storage & clean-up if the program needs them.")
    if skipped:
        notes.append(f"{skipped} file(s) could not be read and were skipped.")
    return notes


def prepare(source, dest: Path, limits, entry: str = "") -> Optional[List[str]]:
    """Notes of the copy made, or None = nothing special to do: the classic code copies (or refuses) as before."""
    cfg = _storage.load()
    budget = int(limits.max_workspace_mb) * MB
    cap = int(cfg["max_file_mb"]) * MB
    narrow_at = int(cfg["quick_narrow_mb"]) * MB
    trim = bool(cfg["trim_large"]) or QUICK.get()
    lean = bool(cfg.get("lean_copy", True))
    try:
        src = Src(source)
    except _Unreadable:
        return None
    try:
        if src.kind == "file":
            if src.single.lower().endswith(".py"):
                return _narrow(src, dest, src.single, max(budget, MB), cap, narrow_at, lean=lean)
            dest.mkdir(parents=True, exist_ok=True)
            e = next(x for x in src.entries if x.rel == src.single)
            src.copy(e, dest, budget)
            _open_dirs(dest)
            return [f"Only the chosen file {src.single} was copied into the container."]
        if entry:
            if lean or src.total > narrow_at or src.total > budget:
                return _narrow(src, dest, entry, budget, cap, narrow_at, lean=lean)
            return None
        if src.total <= budget:
            return None
        if not trim:
            return None                                     # classic refusal (its message points at the options)
        return _trim(src, dest, budget, cap)
    finally:
        src.close()
