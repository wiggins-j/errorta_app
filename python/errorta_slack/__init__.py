"""Errorta Slack PM bridge.

Optional subsystem that lets Errorta post progress updates to, and take
directives from, Slack channels bound to coding projects. Disabled by
default. This package (and its `config` module) MUST NOT import
`slack_sdk` or any other optional dependency at module load time — those
imports belong in the modules that actually need the Slack client, so the
rest of the sidecar keeps working when `slack-sdk` isn't installed.
"""
from __future__ import annotations

SLACK_API_VERSION = 1

__all__ = ["SLACK_API_VERSION"]
