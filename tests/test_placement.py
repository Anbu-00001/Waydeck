from pathlib import Path

from waydeck import placement


def test_bundled_extension_is_complete():
    src = placement.bundled_extension_dir()
    assert (src / "metadata.json").is_file()
    assert (src / "extension.js").is_file()


def test_bundled_metadata_matches_uuid():
    import json

    meta = json.loads((placement.bundled_extension_dir() / "metadata.json").read_text())
    assert meta["uuid"] == placement.EXT_UUID
    # GNOME requires the install dir name to equal the uuid.
    assert placement.bundled_extension_dir().name == placement.EXT_UUID
    assert placement.user_extension_dir().name == placement.EXT_UUID


def test_user_extension_dir_respects_home():
    d = placement.user_extension_dir(home=Path("/tmp/fakehome"))
    assert str(d) == f"/tmp/fakehome/.local/share/gnome-shell/extensions/{placement.EXT_UUID}"
