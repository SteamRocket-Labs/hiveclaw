"""The platform bootstrap template is a projection, not a second role authority."""

_PERSONALITY_AND_BOUNDARIES = """

## Personality
- Diligent and detail-oriented
- Proactively reports work progress
- Asks for confirmation when encountering uncertain information

## Boundaries
- Follows company confidentiality policies
- Sensitive operations require creator approval
"""


def build_bootstrap_soul(agent_name: str, role_description: str) -> str:
    return (
        f"# Soul — {agent_name}\n\n## Identity\n- Name: {agent_name}\n- Role: {role_description}"
        + _PERSONALITY_AND_BOUNDARIES
    )


def project_bootstrap_soul_role(soul_text: str, role_description: str) -> str:
    text = soul_text.strip()
    titled = text.startswith("# Soul — ")
    body = text.partition("\n\n")[2] if titled else text
    prefix = "## Identity\n- Name: "
    if not body.startswith(prefix):
        return soul_text
    name, separator, role_and_tail = body[len(prefix):].partition("\n- Role: ")
    suffix = _PERSONALITY_AND_BOUNDARIES.rstrip()
    if not separator or not role_and_tail.endswith(suffix):
        return soul_text
    previous_role = role_and_tail[:-len(suffix)]
    expected = build_bootstrap_soul(name, previous_role).strip()
    # The soul context resource removes the file's title before identity assembly.
    if text != (expected if titled else expected.partition("\n\n")[2]):
        return soul_text
    projected = build_bootstrap_soul(name, role_description or "").strip()
    return projected if titled else projected.partition("\n\n")[2]
