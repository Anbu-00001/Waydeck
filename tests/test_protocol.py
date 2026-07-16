from waydeck.server import protocol as p


def test_frame_roundtrip():
    payload = b"\x00\x01\x02jpeg-ish"
    frame = p.pack_video_frame(payload, keyframe=True, send_time_ms=1234.5)
    ftype, key, ts = p.unpack_header(frame)
    assert ftype == p.FRAME_TYPE_VIDEO
    assert key is True
    assert ts == 1234.5
    assert frame[p.HEADER_SIZE:] == payload


def test_delta_frame_flag():
    frame = p.pack_video_frame(b"x", keyframe=False, send_time_ms=0.0)
    _, key, _ = p.unpack_header(frame)
    assert key is False


def test_token_check():
    assert p.token_ok("secret", "secret")
    assert not p.token_ok("secret", "wrong")
    assert not p.token_ok("secret", None)
    assert not p.token_ok("secret", "")


def _hello(webcodecs: bool, secure: bool) -> p.ClientHello:
    return p.ClientHello(webcodecs=webcodecs, secure=secure)


def test_transport_auto_prefers_h264_when_possible():
    t, err = p.decide_transport("auto", _hello(True, True), h264_available=True)
    assert (t, err) == (p.TRANSPORT_H264, None)


def test_transport_auto_falls_back_without_secure_context():
    t, err = p.decide_transport("auto", _hello(True, False), h264_available=True)
    assert (t, err) == (p.TRANSPORT_JPEG, None)


def test_transport_auto_falls_back_without_encoder():
    t, err = p.decide_transport("auto", _hello(True, True), h264_available=False)
    assert (t, err) == (p.TRANSPORT_JPEG, None)


def test_transport_forced_h264_errors_clearly():
    t, err = p.decide_transport("h264", _hello(False, False), h264_available=True)
    assert t == "" and "secure context" in err
    t, err = p.decide_transport("h264", _hello(True, True), h264_available=False)
    assert t == "" and "encoder" in err


def test_transport_forced_jpeg_always_works():
    t, err = p.decide_transport("jpeg", _hello(True, True), h264_available=True)
    assert (t, err) == (p.TRANSPORT_JPEG, None)


def test_hello_parsing_is_defensive():
    hello = p.ClientHello.from_msg({"webcodecs": 1, "secure": None, "ua": "x" * 500})
    assert hello.webcodecs is True
    assert hello.secure is False
    assert len(hello.user_agent) == 200
