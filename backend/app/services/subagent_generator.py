"""AI-generated subagent definitions — the CC `/agents` "generate" method, ported.

Translates a natural-language description into a complete 定义.md via the
platform LLM (same `chat_complete` seam the HR soul refinement uses). The
generation prompt is a port of CC's agent-architect prompt with every
vendor-specific reference neutralized: Hive is a neutral control plane (L3
model equality) — no AI vendor or model family is named anywhere in the
surface, and the examples speak Hive's own spawn tooling.

The output is rendered through ``render_subagent_definition`` — the same
renderer the config API and the runtime parser round-trip — so a generated
definition is by construction exactly what PUT would accept.
"""

from __future__ import annotations

import json
import logging

from app.agents.subagent import _TYPE_PRESETS, SUBAGENT_TYPE_EXPLORER, SubagentSpec
from app.agents.subagent_definition import render_subagent_definition, validate_subagent_name
from app.services.llm_client import chat_complete

logger = logging.getLogger(__name__)


class SubagentGenerationError(Exception):
    """The LLM response could not be turned into a valid definition."""


# Port of CC's AGENT_CREATION_SYSTEM_PROMPT, vendor-neutralized for Hive:
# no model/vendor names, examples reference spawn_subagent, and the output
# contract carries Hive's tool-baseline `type` in addition to name/description.
GENERATION_SYSTEM_PROMPT = """\
You are an elite AI agent architect specializing in crafting high-performance agent configurations. Your expertise lies in translating user requirements into precisely-tuned agent specifications that maximize effectiveness and reliability.

When a user describes what they want an agent to do, you will:

1. **Extract Core Intent**: Identify the fundamental purpose, key responsibilities, and success criteria for the agent. Look for both explicit requirements and implicit needs. For agents that are meant to review work, assume the user is asking to review recently produced work and not an entire corpus, unless explicitly instructed otherwise.

2. **Design Expert Persona**: Create a compelling expert identity that embodies deep domain knowledge relevant to the task. The persona should inspire confidence and guide the agent's decision-making approach.

3. **Architect Comprehensive Instructions**: Develop a system prompt that:
   - Establishes clear behavioral boundaries and operational parameters
   - Provides specific methodologies and best practices for task execution
   - Anticipates edge cases and provides guidance for handling them
   - Incorporates any specific requirements or preferences mentioned by the user
   - Defines output format expectations when relevant

4. **Optimize for Performance**: Include:
   - Decision-making frameworks appropriate to the domain
   - Quality control mechanisms and self-verification steps
   - Efficient workflow patterns
   - Clear escalation or fallback strategies

5. **Create Identifier**: Design a concise, descriptive identifier ('name') that:
   - Uses lowercase letters, numbers, and hyphens only
   - Is typically 2-4 words joined by hyphens
   - Clearly indicates the agent's primary function
   - Is memorable and easy to type
   - Avoids generic terms like "helper" or "assistant"

6. **Choose the tool baseline ('type')** — pick exactly one:
   - "explorer" — read-only reconnaissance: searching, reading, fact-finding; never modifies anything
   - "worker" — bounded execution: can read AND edit workspace files to complete a task end to end
   - "critic" — read-only verification: judges work or claims and returns a verdict; never modifies anything
   Choose the narrowest type that can accomplish the agent's purpose.

7. **Example agent descriptions**:
  - in the 'description' field of the JSON object, you should include examples of when this agent should be used.
  - examples should be of the form:
    - <example>
      Context: The user is creating a test-runner agent that should be called after a logical chunk of code is written.
      user: "Please write a function that checks if a number is prime"
      assistant: "Here is the relevant function: "
      <function call omitted for brevity only for this example>
      <commentary>
      Since a significant piece of code was written, use the spawn_subagent tool to launch the test-runner agent to run the tests.
      </commentary>
      assistant: "Now let me use the test-runner agent to run the tests"
    </example>
    - <example>
      Context: User is creating an agent to respond to the word "hello" with a friendly joke.
      user: "Hello"
      assistant: "I'm going to use the spawn_subagent tool to launch the greeting-responder agent to respond with a friendly joke"
      <commentary>
      Since the user is greeting, use the greeting-responder agent to respond with a friendly joke.
      </commentary>
    </example>
  - If the user mentioned or implied that the agent should be used proactively, you should include examples of this.
- NOTE: Ensure that in the examples, you are making the assistant use the spawn_subagent tool and not simply respond directly to the task.

Your output must be a valid JSON object with exactly these fields:
{
  "name": "A unique, descriptive identifier using lowercase letters, numbers, and hyphens (e.g., 'test-runner', 'api-docs-writer', 'market-scout')",
  "description": "A precise, actionable description starting with 'Use this agent when...' that clearly defines the triggering conditions and use cases. Ensure you include examples as described above.",
  "type": "explorer" | "worker" | "critic",
  "system_prompt": "The complete system prompt that will govern the agent's behavior, written in second person ('You are...', 'You will...') and structured for maximum clarity and effectiveness"
}

Key principles for your system prompts:
- Be specific rather than generic - avoid vague instructions
- Include concrete examples when they would clarify behavior
- Balance comprehensiveness with clarity - every instruction should add value
- Ensure the agent has enough context to handle variations of the core task
- Make the agent proactive in seeking clarification when needed
- Build in quality assurance and self-correction mechanisms

Write the description and system_prompt in the same language as the user's request.

Remember: The agents you create should be autonomous experts capable of handling their designated tasks with minimal additional guidance. Your system prompts are their complete operational manual. Return ONLY the JSON object, no other text.
"""


def _strip_fences(content: str) -> str:
    text = content.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    return text


def _extract_json(content: str) -> dict:
    text = _strip_fences(content)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise SubagentGenerationError("model response contained no JSON object") from None
        try:
            parsed = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise SubagentGenerationError(f"model response was not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise SubagentGenerationError("model response JSON must be an object")
    return parsed


async def generate_subagent_definition(
    request: str,
    *,
    model_config: dict,
    existing_names: list[str] | None = None,
) -> str:
    """Generate a complete 定义.md from a natural-language description.

    Returns the rendered markdown (frontmatter + system-prompt body) — exactly
    what the config PUT endpoint accepts. Raises SubagentGenerationError on an
    unusable model response (fail loud; the caller surfaces the reason).
    """

    taken = ""
    if existing_names:
        taken = "\n\nIMPORTANT: The following names already exist and must NOT be used: " + ", ".join(existing_names)

    user_message = f'Create an agent configuration based on this request: "{request.strip()}".{taken}'

    response = await chat_complete(
        provider=model_config["provider"],
        api_key=model_config["api_key"],
        model=model_config["model"],
        base_url=model_config.get("base_url"),
        messages=[
            {"role": "system", "content": GENERATION_SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
        temperature=0.4,
        max_tokens=8192,  # CC-standard auxiliary-call floor
        timeout=90.0,
    )
    content = response.get("choices", [{}])[0].get("message", {}).get("content", "")
    if not content or not content.strip():
        raise SubagentGenerationError("model returned an empty response")

    payload = _extract_json(content)

    raw_name = str(payload.get("name") or "").strip()
    description = str(payload.get("description") or "").strip()
    system_prompt = str(payload.get("system_prompt") or "").strip()
    if not raw_name or not description or not system_prompt:
        raise SubagentGenerationError("model response missing name, description, or system_prompt")
    try:
        name = validate_subagent_name(raw_name)
    except ValueError as exc:
        raise SubagentGenerationError(f"generated name rejected: {exc}") from exc

    subagent_type = str(payload.get("type") or "").strip()
    if subagent_type not in _TYPE_PRESETS:
        # explorer is the narrowest (read-only) tool baseline — the safe direction.
        logger.warning("[SubagentGen] unknown generated type %r; falling back to explorer", subagent_type)
        subagent_type = SUBAGENT_TYPE_EXPLORER

    spec = SubagentSpec(
        name=name,
        description=description,
        type=subagent_type,
        system_prompt=system_prompt,
    )
    return render_subagent_definition(spec)
