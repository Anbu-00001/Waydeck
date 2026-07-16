from waydeck.usb import adb


def test_parse_devices():
    out = """List of devices attached
abc123\tdevice
def456\tunauthorized
ghi789\toffline

"""
    assert adb.parse_devices(out) == ["abc123"]


def test_parse_devices_empty():
    assert adb.parse_devices("List of devices attached\n\n") == []


def test_command_builders():
    assert adb.build_reverse_cmd("adb", "abc", 8420) == [
        "adb", "-s", "abc", "reverse", "tcp:8420", "tcp:8420"
    ]
    assert adb.build_reverse_remove_cmd("adb", "abc", 8420) == [
        "adb", "-s", "abc", "reverse", "--remove", "tcp:8420"
    ]
    cmd = adb.build_open_url_cmd("adb", "abc", "http://localhost:8420/?t=x")
    assert cmd[:6] == ["adb", "-s", "abc", "shell", "am", "start"]
    assert cmd[-1] == "http://localhost:8420/?t=x"
