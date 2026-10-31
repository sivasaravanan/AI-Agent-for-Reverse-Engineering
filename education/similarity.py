"""Source similarity between submissions (token-shingle overlap).

Identifiers and strings are normalised (numbers are kept) so renaming variables does not hide copying. Shingles shared by many
submissions (or present in an optional starter template) are ignored so boilerplate is not flagged. The result is a
LEAD for human review: shared code can come from the assignment, a tutorial or a common solution.
"""
from __future__ import annotations

import re
import zlib
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Set, Tuple

from education.rubric import SOURCE_EXT

_KEYWORDS = set("""
and as assert async await break case catch class const continue def default del do elif else enum except extends
finally for foreach from func function global go if implements import in instanceof interface is lambda let namespace
new nil none not null or package pass private protected public raise return select self static struct super switch
this throw throws try type typeof using var void while with yield true false int float double string bool boolean char
long short byte list dict set tuple print println echo require include include_once use fn match impl trait mod pub
""".split())
_TOKEN = re.compile(r"[A-Za-z_]\w*|\d+(?:\.\d+)?|==|!=|<=|>=|&&|\|\||->|=>|\+\+|--|[^\s\w]")
_C_COMMENT = re.compile(r"/\*.*?\*/|//[^\n]*", re.S)
_HASH_COMMENT = re.compile(r"#[^\n]*")
_STRING = re.compile(r"\"(?:\\.|[^\"\\\n])*\"|'(?:\\.|[^'\\\n])*'")
_PY_DOC = re.compile(r'""".*?"""|\'\'\'.*?\'\'\'', re.S)


@dataclass
class SimilarityOptions:
    shingle: int = 8                 # tokens per shingle
    min_shingles: int = 40           # submissions with fewer shingles are not compared
    common_share: float = 0.5        # shingles held by more than this share of submissions are boilerplate (needs >= 4 submissions)
    review_threshold: float = 0.35   # Jaccard score to list a pair "review"
    high_threshold: float = 0.60
    max_pairs: int = 200


@dataclass
class PairResult:
    a: str
    b: str
    score: float
    containment: float
    shared: int
    level: str
    files: List[Dict[str, object]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, object]:
        return {"a": self.a, "b": self.b, "score": round(self.score, 3), "containment": round(self.containment, 3),
                "shared_shingles": self.shared, "level": self.level, "top_file_pairs": self.files}


def tokens(name: str, text: str) -> List[str]:
    ext = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext in (".py", ".rb"):
        text = _PY_DOC.sub(" ", text)
        text = _HASH_COMMENT.sub(" ", _STRING.sub('"S"', text)) if ext == ".py" else _HASH_COMMENT.sub(" ", text)
    elif ext == ".sql":
        text = re.sub(r"--[^\n]*", " ", text)
    else:
        text = _C_COMMENT.sub(" ", text)
    text = _STRING.sub('"S"', text)
    out: List[str] = []
    for t in _TOKEN.findall(text):
        if t[0].isalpha() or t[0] == "_":
            out.append(t.lower() if t.lower() in _KEYWORDS else "I")
        elif t[0].isdigit():
            out.append(t)            # numeric literals are kept: they distinguish independent solutions of similar shape
        else:
            out.append(t)
    return out


def _shingles(toks: List[str], k: int) -> Set[int]:
    if len(toks) < k:
        return set()
    return {zlib.crc32(" ".join(toks[i:i + k]).encode()) for i in range(len(toks) - k + 1)}


def source_texts(texts: Dict[str, str]) -> Dict[str, str]:
    from core.archive_manager import is_vendored
    return {n: t for n, t in texts.items() if ("." + n.rsplit(".", 1)[-1].lower()) in SOURCE_EXT and not is_vendored(n)
            and "." in n}


def compare_submissions(subs: Dict[str, Dict[str, str]], opts: Optional[SimilarityOptions] = None,
                        template: Optional[Dict[str, str]] = None) -> Tuple[List[PairResult], List[str]]:
    """``subs``: student id -> {file name: text}. Returns (pairs sorted by score, notes)."""
    o = opts or SimilarityOptions()
    notes: List[str] = []
    per_file: Dict[str, Dict[str, Set[int]]] = {}
    whole: Dict[str, Set[int]] = {}
    for sid, files in subs.items():
        pf = {n: _shingles(tokens(n, t), o.shingle) for n, t in source_texts(files).items()}
        per_file[sid] = pf
        whole[sid] = set().union(*pf.values()) if pf else set()
    ignore: Set[int] = set()
    if template:
        for n, t in source_texts(template).items():
            ignore |= _shingles(tokens(n, t), o.shingle)
        notes.append(f"Starter template excluded ({len(ignore)} shingles).")
    eligible = [s for s, w in whole.items() if len(w) >= o.min_shingles]
    skipped = sorted(set(subs) - set(eligible))
    if skipped:
        notes.append(f"Too little source code to compare: {', '.join(skipped[:20])}{' ...' if len(skipped) > 20 else ''}.")
    holders: Dict[int, List[str]] = {}
    for s in eligible:
        for h in whole[s]:
            holders.setdefault(h, []).append(s)
    if len(eligible) >= 4:
        limit = o.common_share * len(eligible)
        common = {h for h, v in holders.items() if len(v) > limit}
        if common:
            notes.append(f"{len(common)} shingles shared by most submissions were treated as boilerplate.")
        ignore |= common
    counts: Dict[Tuple[str, str], int] = {}
    for h, v in holders.items():
        if h in ignore or len(v) < 2:
            continue
        for i in range(len(v)):
            for j in range(i + 1, len(v)):
                key = (v[i], v[j]) if v[i] < v[j] else (v[j], v[i])
                counts[key] = counts.get(key, 0) + 1
    size = {s: len(whole[s] - ignore) for s in eligible}
    pairs: List[PairResult] = []
    for (a, b), shared in counts.items():
        union = size[a] + size[b] - shared
        if union <= 0:
            continue
        score = shared / union
        if score < min(0.15, o.review_threshold):
            continue
        cont = shared / max(1, min(size[a], size[b]))
        level = "high" if score >= o.high_threshold else ("review" if score >= o.review_threshold else "low")
        pairs.append(PairResult(a, b, score, cont, shared, level))
    pairs.sort(key=lambda p: (-p.score, p.a, p.b))
    pairs = pairs[:o.max_pairs]
    for p in pairs:
        if p.level == "low":
            continue
        fp = []
        for fa, sa in per_file[p.a].items():
            sa = sa - ignore
            if not sa:
                continue
            for fb, sb in per_file[p.b].items():
                sb = sb - ignore
                inter = len(sa & sb)
                if inter >= 5:
                    fp.append({"file_a": fa, "file_b": fb, "shared_shingles": inter, "overlap": round(inter / max(1, min(len(sa), len(sb))), 3)})
        fp.sort(key=lambda x: -x["shared_shingles"])
        p.files = fp[:5]
    notes.append("Similarity is a lead for human review, not proof of copying: shared code can come from the assignment, "
                 "a tutorial or a common solution.")
    return pairs, notes
