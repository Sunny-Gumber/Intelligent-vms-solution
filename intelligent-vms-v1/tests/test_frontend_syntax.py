from pathlib import Path

import pytest

from tools.check_frontend_syntax import check_frontend


WEB = Path(__file__).parents[1] / "web" / "index.html"


def test_complete_frontend_and_handlers_parse():
    assert check_frontend(WEB.read_text(encoding="utf-8")) == []


@pytest.mark.parametrize("broken", [
    "const info=await jsonFetch(${API}+'/auth/session');",
    "const r=await fetch(${API}+'/auth/session',{});",
    "try{await apiFetch(${API}+'/auth/session',{})}catch{}",
    "await jsonFetch(${API}+'/cameras',{});",
    r"const whep=access.webrtc_url.replace(/\\/$/,'')+'/whep';",
])
def test_prior_broken_expressions_are_rejected(broken):
    assert check_frontend("<script>async function regression(){" + broken + "}</script>")


def test_reintroduced_defect_in_complete_page_is_rejected():
    html = WEB.read_text(encoding="utf-8")
    fixed = "const info=await jsonFetch(`${API}/auth/session`);"
    assert html.count(fixed) == 1
    assert check_frontend(html.replace(fixed, "const info=await jsonFetch(${API}+'/auth/session');"))


def test_visible_escape_is_rejected_but_javascript_escape_is_valid():
    assert check_frontend(r"<div>\n</div><script>const value='ok';</script>")
    assert not check_frontend(r"<script>const newline='\n';</script>")


def test_inline_handler_is_checked_with_function_scope():
    assert check_frontend('<button onclick="fetch(${API})"></button><script>const API="/api";</script>')
    assert not check_frontend('<button onclick="return false"></button><script>const API="/api";</script>')


def test_missing_or_unclosed_script_fails_closed():
    assert check_frontend("<div>No application</div>")
    assert check_frontend("<script>const unfinished=true;")


def test_unsupported_external_script_fails_closed():
    with pytest.raises(ValueError, match="External scripts"):
        check_frontend('<script src="external.js"></script>')


@pytest.mark.parametrize("source", ["https://evil.invalid/a.js", "//evil/a.js", "../forensic-ui.js", "forensic-ui.js?x"])
def test_external_script_policy_rejects_remote_and_traversal(source):
    """Only exact reviewed local asset names may be loaded by the gate."""
    with pytest.raises(ValueError, match="External scripts"):
        check_frontend(f'<script src="{source}"></script>')


def test_local_script_contents_are_parsed(monkeypatch, tmp_path):
    """Approved names do not bypass JavaScript syntax validation."""
    import tools.check_frontend_syntax as checker
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "forensic-ui.js").write_text("const broken = ;")
    monkeypatch.setattr(checker, "ROOT", tmp_path)
    assert checker.check_frontend('<script>let ok=1;</script><script src="forensic-ui.js"></script>')
