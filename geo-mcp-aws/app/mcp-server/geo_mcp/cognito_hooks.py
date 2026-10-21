"""Cognito trigger for open sign-up (runs in the jobs image, outside the VPC).

PostConfirmation (after the user enters the emailed code):
  - adds the account to the demo groups (DEMO_GROUPS, default geo-reader,geo-writer,geo-demo), so
    every self-registered account gets guest limits, whichever client confirmed it
  - emails the owner through SNS (ALERT_TOPIC_ARN): "New demo sign-up: <email>"
A failed notification never blocks the sign-up; a failed group assignment does (the user
can't use the demo without it, so Cognito reports the error and they can retry).
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

GROUPS = [g.strip() for g in os.environ.get("DEMO_GROUPS", "geo-reader,geo-writer,geo-demo").split(",") if g.strip()]
TOPIC = os.environ.get("ALERT_TOPIC_ARN", "")
_idp = _sns = None


def handler(event, context):
    global _idp, _sns
    if event.get("triggerSource") != "PostConfirmation_ConfirmSignUp":
        return event
    import boto3
    _idp = _idp or boto3.client("cognito-idp")
    pool, user = event["userPoolId"], event["userName"]
    for g in GROUPS:
        _idp.admin_add_user_to_group(UserPoolId=pool, Username=user, GroupName=g)
    email = (event.get("request", {}).get("userAttributes") or {}).get("email", user)
    print(f"sign-up confirmed: {email} -> {', '.join(GROUPS)}", flush=True)
    if TOPIC:
        try:
            _sns = _sns or boto3.client("sns")
            when = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            _sns.publish(TopicArn=TOPIC, Subject="Geo demo: new sign-up",
                         Message=f"New demo sign-up: {email}\nGroups: {', '.join(GROUPS)}\nTime: {when}\n\n"
                                 "Disable the account in Cognito if it looks wrong.")
        except Exception as exc:   # never block a sign-up on a notification
            print(f"notification failed: {type(exc).__name__}", flush=True)
    return event
