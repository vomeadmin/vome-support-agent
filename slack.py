import os
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])

CHANNEL_ENGINEERING = os.environ["SLACK_CHANNEL_VOME_SUPPORT_ENGINEERING"]
CHANNEL_FIELD_FEEDBACK = os.environ["SLACK_CHANNEL_VOME_FIELD_FEEDBACK"]
CHANNEL_FEATURE_REQUESTS = os.environ["SLACK_CHANNEL_VOME_FEATURE_REQUESTS"]
CHANNEL_AGENT_LOG = os.environ["SLACK_CHANNEL_VOME_AGENT_LOG"]


def post_to_engineering(message: str) -> dict:
    """Post to #vome-support-engineering for P1 escalations, timing requests, and daily digests."""
    return client.chat_postMessage(channel=CHANNEL_ENGINEERING, text=message)


def post_to_field_feedback(message: str, thread_ts: str | None = None) -> dict:
    """Post to #vome-field-feedback. Use thread_ts to reply inside an existing thread."""
    return client.chat_postMessage(
        channel=CHANNEL_FIELD_FEEDBACK,
        text=message,
        thread_ts=thread_ts,
    )


def post_to_feature_requests(message: str) -> dict:
    """Post to #vome-feature-requests for scored feature request pings and weekly digests."""
    return client.chat_postMessage(channel=CHANNEL_FEATURE_REQUESTS, text=message)


def post_to_log(message: str) -> dict:
    """Post to #vome-agent-log for audit trail of all agent actions."""
    return client.chat_postMessage(channel=CHANNEL_AGENT_LOG, text=message)


def handle_incoming_message(payload: dict) -> dict | None:
    """Process an incoming Slack message event (e.g. Ron posting field feedback).

    Returns the parsed event dict, or None if the message should be ignored
    (bot messages, message_changed subtypes, etc.).
    """
    event = payload.get("event", {})

    # Ignore bot messages to avoid loops
    if event.get("bot_id") or event.get("subtype"):
        return None

    return {
        "channel": event.get("channel"),
        "user": event.get("user"),
        "text": event.get("text", ""),
        "ts": event.get("ts"),
        "thread_ts": event.get("thread_ts"),
    }
