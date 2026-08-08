from incident_response.cli import _build_parser


def test_serve_binds_loopback_by_default():
    args = _build_parser().parse_args(["serve"])

    assert args.host == "127.0.0.1"
