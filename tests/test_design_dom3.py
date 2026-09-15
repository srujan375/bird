"""The editor wrap/unwrap seam: the script rides with the document."""

from bird.harnesses.design import dom


def _html() -> str:
    return "<body><h1>Hello</h1><p>Bye</p></body>"


def test_wrap_injects_the_editor_script_before_body_close():
    wrapped = dom.wrap_editor(_html(), "window.__bird = true")
    assert wrapped == (
        "<body><h1>Hello</h1><p>Bye</p>"
        f'<script {dom.EDITOR_ATTR}>window.__bird = true</script></body>'
    )
    assert dom.EDITOR_ATTR in wrapped


def test_unwrap_strips_the_script_and_round_trips():
    html = _html()
    wrapped = dom.wrap_editor(html, "window.__bird = true")
    assert dom.unwrap_editor(wrapped) == html


def test_wrap_is_idempotent():
    once = dom.wrap_editor(_html(), "window.__bird = true")
    twice = dom.wrap_editor(once, "window.__bird = true")
    assert twice == once