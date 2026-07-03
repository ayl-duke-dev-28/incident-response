"""MockSlackClient's post/update semantics — used by the streaming triage path."""

from incident_response.integrations.slack import MockSlackClient


async def test_update_replaces_text_in_sent():
    slack = MockSlackClient()
    msg = await slack.post(channel="#x", text="v1")
    assert slack.latest_text_for(msg.ts) == "v1"
    await slack.update(channel="#x", ts=msg.ts, text="v2")
    assert slack.latest_text_for(msg.ts) == "v2"
    await slack.update(channel="#x", ts=msg.ts, text="v3")
    assert slack.latest_text_for(msg.ts) == "v3"
    assert len(slack.sent) == 1  # still one message, just rewritten
    assert len(slack.updates) == 2


async def test_update_records_history_independently():
    slack = MockSlackClient()
    msg = await slack.post(channel="#x", text="hi")
    await slack.update(channel="#x", ts=msg.ts, text="a")
    await slack.update(channel="#x", ts=msg.ts, text="b")
    assert [u.text for u in slack.updates] == ["a", "b"]
