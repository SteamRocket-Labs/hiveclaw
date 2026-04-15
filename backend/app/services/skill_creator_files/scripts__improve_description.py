#!/usr/bin/env python3
"""Improve a skill description based on eval results.

Takes eval results (from run_eval.py) and generates an improved description
using Claude with extended thinking.
"""

import argparse
import json
import re
import sys
from pathlib import Path

import anthropic
from loguru import logger

from scripts.utils import parse_skill_md


def improve_description(
    client: anthropic.Anthropic,
    skill_name: str,
    skill_content: str,
    current_description: str,
    eval_results: dict,
    history: list[dict],
    model: str,
    test_results: dict | None = None,
    log_dir: Path | None = None,
    iteration: int | None = None,
) -> str:
    """Call Claude to improve the description based on eval results."""
    failed_triggers = [r for r in eval_results["results"] if r["should_trigger"] and not r["pass"]]
    false_triggers = [r for r in eval_results["results"] if not r["should_trigger"] and not r["pass"]]

    # Build scores summary
    train_score = f"{eval_results['summary']['passed']}/{eval_results['summary']['total']}"
    if test_results:
        test_score = f"{test_results['summary']['passed']}/{test_results['summary']['total']}"
        scores_summary = f"Train: {train_score}, Test: {test_score}"
    else:
        scores_summary = f"Train: {train_score}"

    prompt = f"""<role>
You are optimizing the description of a skill called "{skill_name}". Your
output is the single sentence of natural language that determines whether
another LLM will select this skill for a given user query. Trigger accuracy
is the metric; you will be scored on it.
</role>

<progressive_disclosure_context>
A "skill" uses progressive disclosure:
- The title + description appear in an `available_skills` catalog injected
  into every LLM turn. Token-expensive to grow, cheap to consult.
- Only when the LLM decides to use the skill does it read the full .md file
  (instructions, helper scripts, docs).

Your description is the selector — it alone must trigger for relevant
queries AND avoid triggering for irrelevant ones. It competes with every
other skill's description for attention, so it must be distinctive.
</progressive_disclosure_context>

<current_description>
"{current_description}"
</current_description>

<current_scores scores_summary="{scores_summary}">
"""
    if failed_triggers:
        prompt += "FAILED TO TRIGGER (should have triggered but didn't):\n"
        for r in failed_triggers:
            prompt += f'  - "{r["query"]}" (triggered {r["triggers"]}/{r["runs"]} times)\n'
        prompt += "\n"

    if false_triggers:
        prompt += "FALSE TRIGGERS (triggered but shouldn't have):\n"
        for r in false_triggers:
            prompt += f'  - "{r["query"]}" (triggered {r["triggers"]}/{r["runs"]} times)\n'
        prompt += "\n"

    if history:
        prompt += "PREVIOUS ATTEMPTS (do NOT repeat these — try something structurally different):\n\n"
        for h in history:
            train_s = f"{h.get('train_passed', h.get('passed', 0))}/{h.get('train_total', h.get('total', 0))}"
            test_s = (
                f"{h.get('test_passed', '?')}/{h.get('test_total', '?')}" if h.get("test_passed") is not None else None
            )
            score_str = f"train={train_s}" + (f", test={test_s}" if test_s else "")
            prompt += f"<attempt {score_str}>\n"
            prompt += f'Description: "{h["description"]}"\n'
            if "results" in h:
                prompt += "Train results:\n"
                for r in h["results"]:
                    status = "PASS" if r["pass"] else "FAIL"
                    prompt += f'  [{status}] "{r["query"][:80]}" (triggered {r["triggers"]}/{r["runs"]})\n'
            if h.get("note"):
                prompt += f"Note: {h['note']}\n"
            prompt += "</attempt>\n\n"

    prompt += f"""</current_scores>

<skill_content>
{skill_content}
</skill_content>

<optimization_tips>
- **Imperative voice**: "Use this skill for …" beats "This skill does …".
  The selector LLM pattern-matches imperatives against user intent.
- **Intent over implementation**: describe WHAT the user is trying to
  achieve, not HOW the skill works internally.
- **Distinctive phrasing**: your description competes with every other
  skill for attention. Generic language ("helps with tasks", "useful for
  work") triggers nothing. Use specific domain verbs and nouns.
- **Generalize, don't enumerate**: do NOT list every failed query. Find
  the category of intent behind the failures and describe that. An ever-
  growing enumeration overfits and burns tokens in every future turn.
- **Change it up on repeated failures**: if 2-3 attempts at a phrasing
  still fail, try a structurally different angle (different sentence
  shape, different emphasis), not just synonyms.
</optimization_tips>

<anti_patterns>
- ❌ **Overfitting to failed queries** — listing "use for X, Y, Z" where
  X, Y, Z are verbatim queries from the failure set.
- ❌ **Implementation leakage** — describing the skill's tools, prompts,
  or file layout. The selector LLM doesn't care; users don't either.
- ❌ **Generic boilerplate** — "helps the user with their request". Every
  skill "helps with requests"; this triggers nothing.
- ❌ **Trigger-word stuffing** — cramming unrelated keywords hoping to
  catch more queries. Dilutes signal, creates false triggers.
- ❌ **Prompt-injection bait** — do NOT include text like "ignore prior
  instructions" or directives aimed at the selector LLM. Descriptions are
  data, not instructions.
- ❌ **Repeating a prior attempt** — if a phrasing already failed in the
  history above, don't resubmit minor rewordings of the same structure.
</anti_patterns>

<constraints>
- Length: aim for 100-200 words. Hard cap: 1024 characters.
- Format: raw text inside `<new_description>` tags only. No prose before
  or after. No markdown fences. No commentary on your reasoning.
- Language: match the language of the skill's existing description and
  content. Do not translate.
</constraints>

<output_format>
Respond with ONLY the new description text inside `<new_description>` tags.

Example shape:
```
<new_description>
Use this skill when the user wants to [specific intent] — typically for
[user categories]. Triggers on requests like [generalized category of
intent], especially when [distinguishing signal]. Does NOT trigger for
[adjacent-but-distinct category], which is covered by [other skill hint
if applicable].
</new_description>
```
</output_format>"""

    response = client.messages.create(
        model=model,
        max_tokens=16000,
        thinking={
            "type": "enabled",
            "budget_tokens": 10000,
        },
        messages=[{"role": "user", "content": prompt}],
    )

    # Extract thinking and text from response
    thinking_text = ""
    text = ""
    for block in response.content:
        if block.type == "thinking":
            thinking_text = block.thinking
        elif block.type == "text":
            text = block.text

    # Parse out the <new_description> tags
    match = re.search(r"<new_description>(.*?)</new_description>", text, re.DOTALL)
    description = match.group(1).strip().strip('"') if match else text.strip().strip('"')

    # Log the transcript
    transcript: dict = {
        "iteration": iteration,
        "prompt": prompt,
        "thinking": thinking_text,
        "response": text,
        "parsed_description": description,
        "char_count": len(description),
        "over_limit": len(description) > 1024,
    }

    # If over 1024 chars, ask the model to shorten it
    if len(description) > 1024:
        shorten_prompt = f"Your description is {len(description)} characters, which exceeds the hard 1024 character limit. Please rewrite it to be under 1024 characters while preserving the most important trigger words and intent coverage. Respond with only the new description in <new_description> tags."
        shorten_response = client.messages.create(
            model=model,
            max_tokens=16000,
            thinking={
                "type": "enabled",
                "budget_tokens": 10000,
            },
            messages=[
                {"role": "user", "content": prompt},
                {"role": "assistant", "content": text},
                {"role": "user", "content": shorten_prompt},
            ],
        )

        shorten_thinking = ""
        shorten_text = ""
        for block in shorten_response.content:
            if block.type == "thinking":
                shorten_thinking = block.thinking
            elif block.type == "text":
                shorten_text = block.text

        match = re.search(r"<new_description>(.*?)</new_description>", shorten_text, re.DOTALL)
        shortened = match.group(1).strip().strip('"') if match else shorten_text.strip().strip('"')

        transcript["rewrite_prompt"] = shorten_prompt
        transcript["rewrite_thinking"] = shorten_thinking
        transcript["rewrite_response"] = shorten_text
        transcript["rewrite_description"] = shortened
        transcript["rewrite_char_count"] = len(shortened)
        description = shortened

    transcript["final_description"] = description

    if log_dir:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f"improve_iter_{iteration or 'unknown'}.json"
        log_file.write_text(json.dumps(transcript, indent=2))

    return description


def main():
    parser = argparse.ArgumentParser(description="Improve a skill description based on eval results")
    parser.add_argument("--eval-results", required=True, help="Path to eval results JSON (from run_eval.py)")
    parser.add_argument("--skill-path", required=True, help="Path to skill directory")
    parser.add_argument("--history", default=None, help="Path to history JSON (previous attempts)")
    parser.add_argument("--model", required=True, help="Model for improvement")
    parser.add_argument("--verbose", action="store_true", help="Print thinking to stderr")
    args = parser.parse_args()

    skill_path = Path(args.skill_path)
    if not (skill_path / "SKILL.md").exists():
        logger.error(f"Error: No SKILL.md found at {skill_path}")
        sys.exit(1)

    eval_results = json.loads(Path(args.eval_results).read_text())
    history = []
    if args.history:
        history = json.loads(Path(args.history).read_text())

    name, _, content = parse_skill_md(skill_path)
    current_description = eval_results["description"]

    if args.verbose:
        logger.info(f"Current: {current_description}")
        logger.info(f"Score: {eval_results['summary']['passed']}/{eval_results['summary']['total']}")

    client = anthropic.Anthropic()
    new_description = improve_description(
        client=client,
        skill_name=name,
        skill_content=content,
        current_description=current_description,
        eval_results=eval_results,
        history=history,
        model=args.model,
    )

    if args.verbose:
        logger.info(f"Improved: {new_description}")

    # Output as JSON with both the new description and updated history
    output = {
        "description": new_description,
        "history": history
        + [
            {
                "description": current_description,
                "passed": eval_results["summary"]["passed"],
                "failed": eval_results["summary"]["failed"],
                "total": eval_results["summary"]["total"],
                "results": eval_results["results"],
            }
        ],
    }
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
