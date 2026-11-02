"""The walkthrough link on Home: shown only for an https URL, escaped."""
from app.ui_home import walkthrough_link


def test_empty_url_shows_nothing():
    assert walkthrough_link("") == ""


def test_non_https_is_refused():
    assert walkthrough_link("javascript:alert(1)") == ""
    assert walkthrough_link("http://youtube.com/watch?v=x") == ""


def test_https_url_renders_a_new_tab_link():
    html = walkthrough_link("https://youtu.be/abc123")
    assert 'data-testid="walkthrough-link"' in html
    assert 'href="https://youtu.be/abc123"' in html
    assert 'target="_blank"' in html and 'rel="noopener"' in html
    assert "Watch the walkthrough" in html


def test_the_url_is_escaped():
    html = walkthrough_link('https://x.test/"><script>')
    assert "<script>" not in html and "&quot;" in html
