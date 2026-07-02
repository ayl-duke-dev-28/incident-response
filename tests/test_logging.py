import io
import json
import logging

from incident_response.logging_config import (
    JsonFormatter,
    set_incident_id,
    set_trace_id,
)


def _capture(func):
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("test.logger")
    logger.handlers = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False
    func(logger)
    return [json.loads(line) for line in stream.getvalue().strip().splitlines() if line]


def test_json_formatter_emits_structured_fields():
    lines = _capture(lambda log: log.info("hello", extra={"foo": "bar"}))
    assert lines[0]["msg"] == "hello"
    assert lines[0]["level"] == "INFO"
    assert lines[0]["foo"] == "bar"


def test_context_vars_attach_to_log_records():
    set_incident_id("inc-42")
    set_trace_id("t-abc")
    try:
        lines = _capture(lambda log: log.warning("hi"))
    finally:
        set_incident_id(None)
        set_trace_id(None)
    assert lines[0]["incident_id"] == "inc-42"
    assert lines[0]["trace_id"] == "t-abc"
