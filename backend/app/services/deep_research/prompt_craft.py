"""Domain-general prompt-craft constants shared across the Deep Research reasoning prompts.

Borrowed (domain-agnostic) from academic-research-skills:
- WRITING_QUALITY  ← `academic-paper/references/writing_quality_check.md` (anti-AI-slop: kill
  filler words, throat-clearing, rule-of-three padding, synonym cycling, em-dash overuse).
- REASONING_CALIBRATION ← `deep-research/references/argumentation_reasoning_framework.md`
  (Toulmin warrant, epistemic-status calibration, inference-to-best-explanation, causal checks).

These are quality kernels, not academic ceremony — no APA / PRISMA / citation-marker machinery.
They apply to any research domain and are injected into the writer + critic prompts.
"""

from __future__ import annotations

WRITING_QUALITY = (
    "WRITING QUALITY (apply while drafting, not as a separate pass):\n"
    "- Cut throat-clearing openers ('It is important to note that', 'In the realm of', "
    "'In today's rapidly evolving', 'It is worth mentioning'). State the point directly.\n"
    "- Avoid AI-tell filler unless it is genuinely the most precise word: delve, leverage, robust, "
    "comprehensive, nuanced, pivotal, crucial, foster, showcase, tapestry, landscape, realm, underscore, "
    "multifaceted, cutting-edge, groundbreaking, paradigm, synergy, holistic, streamline. Choose the concrete word.\n"
    "- Vary sentence and paragraph length. Do NOT pad every list to exactly three items — use as many points "
    "as the evidence warrants (two strong points beat three padded ones).\n"
    "- Keep one term per concept (no synonym cycling); use at most a few em dashes.\n"
    "- No meta-commentary ('This section will discuss…') — just write the content."
)

REASONING_CALIBRATION = (
    "REASONING DISCIPLINE (calibrate every material claim):\n"
    "- Give the WARRANT, not just the data: state why the evidence supports the claim. Data without a warrant "
    "is just information.\n"
    "- Label each major claim's epistemic status and match the language to it: established ('X is …'), "
    "supported ('evidence indicates …'), preliminary ('early / limited evidence suggests …'), inferred "
    "('we infer …'), contested ('sources disagree …'). Never use established language for preliminary evidence.\n"
    "- When more than one explanation fits the same evidence, address the strongest ALTERNATIVE explanation "
    "explicitly rather than asserting only the convenient one.\n"
    "- For causal claims, verify the cause precedes the effect and name a plausible mechanism before asserting "
    "causation; otherwise downgrade to correlation."
)
