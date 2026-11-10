"""Optional plain-English explanations using the Anthropic API.

Privacy: the prompt contains only invoice IDs, rule names and numbers. No bank details,
no vendor names. If there is no API key, or the call fails, a template is used instead.
"""
import json
import os
import urllib.request

API_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = "claude-haiku-5-5"  # override with ANTHROPIC_MODEL


def build_prompt(anomalies: list[dict]) -> str:
    lines = [f"- invoice {a['invoice_id']}: [{a['severity']}] {a['rule']}: {a['detail']}"
             for a in anomalies[:20]]
    return ("You are helping an accounts-payable reviewer. For each flagged invoice below, "
            "explain in one or two plain sentences why it was flagged and what to check next. "
            "Do not accuse anyone of fraud; these are signals for human review.\n\n"
            + "\n".join(lines))


def fallback_text(anomalies: list[dict]) -> str:
    return "\n".join(f"{a['invoice_id']} [{a['severity']}] {a['rule']}: {a['detail']}"
                     for a in anomalies[:20])


def _default_post(url: str, headers: dict, body: dict) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers,
                                 method="POST")
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.load(resp)


def explain(anomalies: list[dict], api_key: str | None = None, post=_default_post) -> str:
    if not anomalies:
        return "No anomalies to explain."
    if not api_key:
        return fallback_text(anomalies)
    try:
        resp = post(
            API_URL,
            {"x-api-key": api_key, "anthropic-version": "2023-06-01",
             "content-type": "application/json"},
            {"model": os.environ.get("ANTHROPIC_MODEL", DEFAULT_MODEL), "max_tokens": 600,
             "messages": [{"role": "user", "content": build_prompt(anomalies)}]})
        text = "".join(b.get("text", "") for b in resp["content"] if b.get("type") == "text")
        return text or fallback_text(anomalies)
    except Exception:  # network, auth or parsing problems must never break the endpoint
        return fallback_text(anomalies)
