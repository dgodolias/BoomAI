import logging

import httpx

from boomai.config import settings
from boomai.models import ReviewSummary

logger = logging.getLogger(__name__)


async def send_slack_alert(
    summary: ReviewSummary, pr_url: str, pr_title: str
):
    """Send Slack alert for critical issues found in a PR."""
    if not settings.slack_enabled or not settings.slack_webhook_url:
        logger.info("Slack notifications disabled, skipping")
        return

    if not summary.has_critical:
        logger.info("No critical issues, skipping Slack alert")
        return

    message = {
        "blocks": [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"BoomAI Alert: {summary.critical_count} Critical Issue(s)",
                },
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*PR:* <{pr_url}|{pr_title}>\n"
                        f"*Critical Issues:* {summary.critical_count}\n"
                        f"*Summary:* {summary.summary[:200]}"
                    ),
                },
            },
        ]
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(settings.slack_webhook_url, json=message)
            if resp.status_code != 200:
                logger.error(f"Slack webhook failed: {resp.status_code} {resp.text}")
            else:
                logger.info("Slack alert sent successfully")
    except Exception as e:
        logger.error(f"Slack notification failed: {e}")
