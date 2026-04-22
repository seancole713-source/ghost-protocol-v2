from core.news import _safe_log_snippet


def test_safe_log_snippet_suppresses_html_payload():
    html = "<!DOCTYPE html>\n<html><head><title>oops</title></head><body>error</body></html>"
    assert _safe_log_snippet(html) == "[html-response-suppressed]"


def test_safe_log_snippet_collapses_whitespace_and_truncates():
    text = "line1\nline2\tline3   " + ("x" * 400)
    out = _safe_log_snippet(text, max_len=20)
    assert "\n" not in out
    assert "\t" not in out
    assert len(out) == 20
