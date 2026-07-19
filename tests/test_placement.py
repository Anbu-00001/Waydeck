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


def test_tiling_warning_silent_when_disabled():
    # Not enabled → never warn, regardless of version.
    assert placement._tiling_warning(enabled=False, shell_ver=(46, 0)) is None
    assert placement._tiling_warning(enabled=False, shell_ver=None) is None


def test_tiling_warning_on_affected_version():
    msg = placement._tiling_warning(enabled=True, shell_ver=(46, 2))
    assert msg and "tiling-assistant" in msg and "--tame-tiling" in msg


def test_tiling_warning_silent_once_fixed():
    # Fixed from GNOME 47 / Ubuntu 24.10 upward.
    assert placement._tiling_warning(enabled=True, shell_ver=(47, 0)) is None
    assert placement._tiling_warning(enabled=True, shell_ver=(48, 1)) is None


def test_tiling_warning_warns_when_version_unknown():
    # If we can't read the shell version, err on the side of warning.
    assert placement._tiling_warning(enabled=True, shell_ver=None) is not None
