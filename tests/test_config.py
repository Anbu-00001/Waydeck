import argparse

import pytest

from waydeck.config import Config, config_from_args, parse_size


def test_defaults():
    cfg = config_from_args([])
    assert cfg.port == 8420
    assert (cfg.width, cfg.height) == (1920, 1080)
    assert cfg.transport == "auto"
    assert cfg.input_mode == "touch"
    assert cfg.usb == "auto"
    assert len(cfg.token) >= 16


def test_tokens_are_random_per_run():
    assert config_from_args([]).token != config_from_args([]).token


def test_explicit_flags():
    cfg = config_from_args(
        ["--port", "9000", "--size", "1080x2400", "--transport", "jpeg",
         "--input", "pointer", "--usb", "off", "--token", "sesame"]
    )
    assert cfg.port == 9000
    assert (cfg.width, cfg.height) == (1080, 2400)
    assert cfg.transport == "jpeg"
    assert cfg.input_mode == "pointer"
    assert cfg.usb == "off"
    assert cfg.token == "sesame"


@pytest.mark.parametrize("bad", ["1920", "axb", "0x0", "99999x100", "1920x", ""])
def test_bad_sizes_rejected(bad):
    with pytest.raises(argparse.ArgumentTypeError):
        parse_size(bad)


def test_size_property():
    assert Config(width=800, height=600).size == (800, 600)
