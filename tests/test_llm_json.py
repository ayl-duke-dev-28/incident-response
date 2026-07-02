import pytest

from incident_response.agents.llm import _extract_json


def test_extract_plain_json():
    assert _extract_json('{"a": 1}') == {"a": 1}


def test_extract_wrapped_json():
    text = "Sure — here you go:\n\n{\"a\": 2, \"b\": [1,2]}\n\nHope this helps!"
    assert _extract_json(text) == {"a": 2, "b": [1, 2]}


def test_extract_raises_when_no_json():
    with pytest.raises(ValueError):
        _extract_json("no json here")
