from deeptutor.app.facade import TurnRequest


def test_turn_request_defaults_to_chinese() -> None:
    request = TurnRequest(content="你好")

    assert request.language == "zh"
    assert request.to_payload()["language"] == "zh"
