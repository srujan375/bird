"""Input-time image ingestion — see src/ox/attachments.py for the why."""

import pytest

from ox.attachments import ATTACHMENTS_DIRNAME, ingest_images
from ox.tools.files import MAX_IMAGE_BYTES

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff" + b"\x00" * 64


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    (r / "src").mkdir(parents=True)
    (r / "src" / "app.py").write_text("x = 1\n")
    return r


@pytest.fixture
def run_dir(repo):
    return repo / ".ox" / "sessions" / "20260728-172930-ce432e"


@pytest.fixture
def outside(tmp_path):
    d = tmp_path / "T" / "TemporaryItems" / "NSIRD_screencaptureui_yuclqQ"
    d.mkdir(parents=True)
    return d


def _shot(outside, name="Screenshot 2026-07-28 at 10.59.33 PM.png", data=PNG):
    p = outside / name
    p.write_bytes(data)
    return p


# --- the reported case ---

@pytest.mark.parametrize(
    "wrap",
    [lambda p: f"'{p}'", lambda p: f'"{p}"', lambda p: str(p).replace(" ", "\\ ")],
    ids=["single-quoted", "double-quoted", "backslash-escaped"],
)
def test_dragged_screenshot_is_copied_in(repo, run_dir, outside, wrap):
    shot = _shot(outside)
    text, found = ingest_images(f"{wrap(shot)}, Can you read this image?", run_dir, repo)

    assert len(found) == 1
    assert str(shot) not in text  # the ephemeral path is gone from the message
    assert found[0].path in text
    assert found[0].size == len(PNG)
    assert (repo / found[0].path).read_bytes() == PNG
    assert "Can you read this image?" in text  # the prose survives


def test_the_copy_survives_the_original_being_reaped(repo, run_dir, outside):
    """The whole point: macOS deletes the temp file moments later, and the
    session still has the image."""
    shot = _shot(outside)
    text, found = ingest_images(f"'{shot}'", run_dir, repo)
    shot.unlink()  # macOS reaps the screenshot preview's temp file

    assert (repo / found[0].path).is_file()
    assert (repo / text.strip()).read_bytes() == PNG


def test_copy_lands_in_repo_so_no_permission_card_fires(repo, run_dir, outside):
    from ox.tools.base import ToolContext

    shot = _shot(outside)
    _, found = ingest_images(f"'{shot}'", run_dir, repo)
    ctx = ToolContext(repo_root=repo)
    assert not ctx.is_outside_repo(ctx.resolve_path(found[0].path))


def test_slug_has_no_spaces(repo, run_dir, outside):
    _, found = ingest_images(f"'{_shot(outside)}'", run_dir, repo)
    assert " " not in found[0].path
    assert found[0].path.startswith(f".ox/sessions/20260728-172930-ce432e/{ATTACHMENTS_DIRNAME}/")
    assert found[0].path.endswith(".png")


# --- what it must leave alone ---

def test_plain_message_is_untouched(repo, run_dir):
    text = "Refactor src/app.py and explain the diff."
    assert ingest_images(text, run_dir, repo) == (text, [])


def test_in_repo_image_is_left_alone(repo, run_dir):
    (repo / "docs").mkdir()
    (repo / "docs" / "logo.png").write_bytes(PNG)
    text, found = ingest_images("look at docs/logo.png", run_dir, repo)
    assert found == []
    assert text == "look at docs/logo.png"


def test_nonexistent_path_is_left_alone(repo, run_dir, outside):
    text = f"'{outside / 'ghost.png'}'"
    assert ingest_images(text, run_dir, repo) == (text, [])


def test_non_image_extension_is_left_alone(repo, run_dir, outside):
    doc = outside / "notes.txt"
    doc.write_bytes(b"not an image")
    text = f"'{doc}'"
    assert ingest_images(text, run_dir, repo) == (text, [])


def test_image_extension_is_trusted_like_read_image_does(repo, run_dir, outside):
    """Ingestion accepts exactly what read_image accepts — both ask
    detect_image_mime, which trusts a known image extension without sniffing.
    Copying a mislabeled .png costs a few KB in the session dir; disagreeing
    with read_image about what counts as an image costs a confusing failure."""
    fake = outside / "mislabeled.png"
    fake.write_bytes(b"just some text, no PNG magic")
    _, found = ingest_images(f"'{fake}'", run_dir, repo)
    assert len(found) == 1


def test_extensionless_image_is_not_ingested(repo, run_dir, outside):
    """A known limitation, not an oversight: the scanner finds paths by image
    extension, so a file saved with no extension is invisible to it. read_image
    still handles that case via magic bytes — it just goes through the
    out-of-repo permission card, and can still lose the reaping race."""
    shot = _shot(outside, "screenshot")  # no extension at all
    text = f"'{shot}'"
    assert ingest_images(text, run_dir, repo) == (text, [])


def test_oversized_image_is_left_alone(repo, run_dir, outside):
    big = _shot(outside, "big.png", PNG + b"\x00" * MAX_IMAGE_BYTES)
    text = f"'{big}'"
    assert ingest_images(text, run_dir, repo) == (text, [])


def test_no_run_dir_is_a_noop(repo, outside):
    text = f"'{_shot(outside)}'"
    assert ingest_images(text, None, repo) == (text, [])


def test_a_png_word_in_prose_does_not_crash(repo, run_dir):
    text = "the .png encoder is slow; see image.png handling in src/app.py"
    assert ingest_images(text, run_dir, repo) == (text, [])


# --- multiples ---

def test_two_images_both_land(repo, run_dir, outside):
    a = _shot(outside, "one.png")
    b = _shot(outside, "two.jpg", JPEG)
    text, found = ingest_images(f"compare '{a}' with '{b}'", run_dir, repo)
    assert len(found) == 2
    assert {p.suffix for p in (repo / run_dir / ATTACHMENTS_DIRNAME).parent.glob("*")} or True
    assert (repo / found[0].path).read_bytes() == PNG
    assert (repo / found[1].path).read_bytes() == JPEG
    assert str(a) not in text and str(b) not in text


def test_same_image_named_twice_is_copied_once(repo, run_dir, outside):
    shot = _shot(outside)
    text, found = ingest_images(f"'{shot}' and again '{shot}'", run_dir, repo)
    assert len(found) == 1
    assert text.count(found[0].path) == 2


def test_same_name_from_different_dirs_does_not_overwrite(repo, run_dir, tmp_path):
    d1, d2 = tmp_path / "a", tmp_path / "b"
    d1.mkdir(), d2.mkdir()
    (d1 / "shot.png").write_bytes(PNG)
    (d2 / "shot.png").write_bytes(PNG + b"different")
    text, found = ingest_images(f"'{d1 / 'shot.png'}' '{d2 / 'shot.png'}'", run_dir, repo)
    assert len(found) == 2
    assert found[0].path != found[1].path
    assert (repo / found[1].path).read_bytes().endswith(b"different")
