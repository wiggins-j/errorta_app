"""Versioned dialect prompts."""

DIGEST_V1_PROMPT = (
    "Deliberation dialect: digest_v1. Respond with one JSON object and no "
    "surrounding prose. The \"v\" field MUST be the string \"digest_v1\" "
    "(literal, in quotes) — not a number, not a round counter. Required "
    "keys: v, position, claims, agree, dispute, delta, open, answer_fragment. "
    "Claims use {id,text,cites,confidence}; confidence is the string "
    "\"high\", \"medium\", or \"low\". Use plain English values. "
    "Example envelope: {\"v\":\"digest_v1\",\"position\":\"...\",\"claims\":[],"
    "\"agree\":[],\"dispute\":[],\"delta\":null,\"open\":[],\"answer_fragment\":\"...\"}."
)

DIGEST_PROMPT_VERSION = "digest_v1"

__all__ = ["DIGEST_PROMPT_VERSION", "DIGEST_V1_PROMPT"]
