from __future__ import annotations

from code_review_agents.lenses.catalog import ALL_LENSES, DIMENSIONS
from code_review_agents.lenses.context import render_context
from code_review_agents.models import ReviewOptions
from code_review_agents.render.markdown import render_markdown
from code_review_agents.scout.snapshot import load_snapshot
from code_review_agents.scout.stack import detect_stack
from code_review_agents.scout.static_checks import run_static_checks
from code_review_agents.scout.structure import compute_structure, render_structure

from .conftest import make_ctx

BRANCHY = (
    "def decide(a, b, c, d, e, f):\n"          # 1  six params
    "    if a and b:\n"                         # 2  +1 if, +1 and
    "        for x in c:\n"                     # 3  +1
    "            while x:\n"                    # 4  +1
    "                if d:\n"                   # 5  +1
    "                    x -= 1\n"
    "    return [y for y in e if y] or f\n"     # 7  +1 comprehension, +1 ifs? (comprehension counts once), +1 or
)


def test_function_metrics(repo_factory):
    snap = load_snapshot(repo_factory({"m.py": BRANCHY}))
    [fn] = compute_structure(snap).functions
    assert (fn.name, fn.line, fn.lines, fn.params) == ("decide", 1, 7, 6)
    assert fn.complexity == 8 and fn.nesting == 4 and fn.hot()


def test_simple_function_is_not_hot(repo_factory):
    snap = load_snapshot(repo_factory({"m.py": "def f(x):\n    return x + 1\n"}))
    [fn] = compute_structure(snap).functions
    assert fn.complexity == 1 and not fn.hot()


PKG = {
    "pkg/__init__.py": "",
    "pkg/models.py": "class M:\n    pass\n",
    "pkg/service.py": "from .models import M\nfrom . import repo\n",
    "pkg/repo.py": "from .models import M\n",
    "pkg/cli.py": "from pkg.service import M\n",
}


def test_internal_import_graph_fan_in_and_out(repo_factory):
    report = compute_structure(load_snapshot(repo_factory(PKG)))
    mods = {m.file: m for m in report.modules}
    assert mods["pkg/models.py"].fan_in == ["pkg/repo.py", "pkg/service.py"]
    assert mods["pkg/service.py"].fan_out == ["pkg/models.py", "pkg/repo.py"]
    assert mods["pkg/cli.py"].fan_out == ["pkg/service.py"]
    assert report.cycles == []


def test_import_cycle_is_a_static_finding_with_verified_lines(repo_factory):
    files = dict(PKG)
    files["pkg/models.py"] = "from .service import thing\nclass M:\n    pass\n"
    snap = load_snapshot(repo_factory(files))
    # models -> service -> models, and service -> repo -> models -> service: one cycle of three
    assert compute_structure(snap).cycles == [["pkg/models.py", "pkg/repo.py", "pkg/service.py"]]
    f = next(f for f in run_static_checks(snap, detect_stack(snap)) if (f.dedupe_key or "").startswith("import-cycle"))
    assert f.lens == "extensibility" and f.severity == "medium"
    for ev in f.evidence:
        assert ev.quote in snap.line(ev.file, ev.line)


def test_structure_goes_into_the_context_pack_and_the_review(repo_factory, tmp_path):
    root = repo_factory({"m.py": BRANCHY})
    text, _ = render_context(make_ctx(root, lens="design-patterns"))
    assert "<structure" in text and "m.py:1 decide - 7 / 8 / 6 / 4" in text
    from code_review_agents.api import run_review
    from code_review_agents.models import LensReport

    class R:
        def run(self, ctx):
            return LensReport(lens=ctx.spec.name, model="fake")

    review = run_review(ReviewOptions(source=str(root), out_dir=str(tmp_path / "o")), runner=R())
    assert review.structure["summary"]["hot_functions"] == 1
    md = render_markdown(review)
    assert "### Structure at a glance" in md and "`decide`" in md
    assert md.index(DIMENSIONS["product"]) < md.index(DIMENSIONS["engineering"]) < md.index(DIMENSIONS["operations"])


def test_every_lens_has_a_known_dimension_and_engineering_lenses_exist():
    assert all(spec.dimension in DIMENSIONS for spec in ALL_LENSES.values())
    eng = {k for k, v in ALL_LENSES.items() if v.dimension == "engineering"}
    assert eng == {"design-patterns", "extensibility", "robustness", "maintainability", "reliability"}


def test_render_structure_handles_a_repo_without_internal_imports(repo_factory):
    text = render_structure(compute_structure(load_snapshot(repo_factory({"a.py": "x = 1\n"}))))
    assert "(no internal imports)" in text and "Import cycles: none" in text
