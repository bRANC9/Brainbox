"""MCP prompts: ready-made instructions filled from the knowledge base.

An IDE offers these as slash-commands; each one arrives with the relevant
approved knowledge already attached, so the answer is grounded in what the
company actually approved rather than in the model's prior.
"""

from __future__ import annotations

from apps.search.services import SearchService

from .errors import MCPError

PROMPTS = [
    {
        "name": "deploy_runbook",
        "description": "Deploy a service, with the approved runbook and conventions attached.",
        "arguments": [
            {"name": "service", "description": "Service or topic name", "required": True}
        ],
    },
    {
        "name": "explain_convention",
        "description": "Explain the team convention on a topic, grounded in approved knowledge.",
        "arguments": [
            {"name": "topic", "description": "Convention topic", "required": True}
        ],
    },
    {
        "name": "onboard_node",
        "description": "Summarise the knowledge under a node or topic.",
        "arguments": [
            {"name": "node", "description": "Node or topic name", "required": True}
        ],
    },
]

_ARGUMENT = {
    "deploy_runbook": "service",
    "explain_convention": "topic",
    "onboard_node": "node",
}

_TEMPLATE = {
    "deploy_runbook": (
        "Prepare the deployment for {subject}. Follow the approved runbook and "
        "conventions below, and cite the sources you rely on."
    ),
    "explain_convention": (
        "Explain our convention for {subject}. Ground every claim in the approved "
        "knowledge below and say explicitly what is not covered."
    ),
    "onboard_node": (
        "Summarise the knowledge under '{subject}'. List the key documents and what "
        "each is for."
    ),
}


def list_prompts() -> dict:
    return {"prompts": PROMPTS}


def get_prompt(ctx, name, arguments) -> dict:
    if name not in _TEMPLATE:
        raise MCPError(-32602, f"Unknown prompt: {name}")
    arguments = arguments or {}
    subject = arguments.get(_ARGUMENT[name])
    if not subject:
        raise MCPError(-32602, f"Missing argument: {_ARGUMENT[name]}")

    results = SearchService.search(
        ctx.user,
        str(subject),
        mode="hybrid",
        status="approved",
        limit=5,
        api_key=ctx.api_key,
    )
    lines = []
    for row in results:
        title = row.get("title") or row.get("path") or "?"
        summary = row.get("summary") or row.get("snippet") or ""
        lines.append(f"- {title} — {summary}".rstrip(" —"))
    context = "\n".join(lines) or "(no approved knowledge found)"

    text = (
        _TEMPLATE[name].format(subject=subject)
        + "\n\nApproved knowledge:\n"
        + context
    )
    description = next(
        prompt["description"] for prompt in PROMPTS if prompt["name"] == name
    )
    return {
        "description": description,
        "messages": [{"role": "user", "content": {"type": "text", "text": text}}],
    }
