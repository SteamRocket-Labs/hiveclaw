#!/usr/bin/env python3
"""
Quick validation script for skills - minimal version
"""

import sys
import re
import yaml
from pathlib import Path

from loguru import logger

def validate_skill(skill_path):
    """Basic validation of a skill"""
    skill_path = Path(skill_path)

    # Check SKILL.md exists
    skill_md = skill_path / 'SKILL.md'
    if not skill_md.exists():
        return False, "SKILL.md not found"

    # Read and validate frontmatter
    content = skill_md.read_text()
    if not content.startswith('---'):
        return False, "No YAML frontmatter found"

    # Extract frontmatter
    match = re.match(r'^---\n(.*?)\n---', content, re.DOTALL)
    if not match:
        return False, "Invalid frontmatter format"

    frontmatter_text = match.group(1)

    # Parse YAML frontmatter
    try:
        frontmatter = yaml.safe_load(frontmatter_text)
        if not isinstance(frontmatter, dict):
            return False, "Frontmatter must be a YAML dictionary"
    except yaml.YAMLError as e:
        return False, f"Invalid YAML in frontmatter: {e}"

    # Define allowed properties
    ALLOWED_PROPERTIES = {
        'name',
        'description',
        'license',
        'allowed-tools',
        'tools',
        'metadata',
        'compatibility',
        'is_system',
    }

    # Check for unexpected properties (excluding nested keys under metadata)
    unexpected_keys = set(frontmatter.keys()) - ALLOWED_PROPERTIES
    if unexpected_keys:
        return False, (
            f"Unexpected key(s) in SKILL.md frontmatter: {', '.join(sorted(unexpected_keys))}. "
            f"Allowed properties are: {', '.join(sorted(ALLOWED_PROPERTIES))}"
        )

    # Check required fields
    if 'name' not in frontmatter:
        return False, "Missing 'name' in frontmatter"
    if 'description' not in frontmatter:
        return False, "Missing 'description' in frontmatter"

    # Extract name for validation
    name = frontmatter.get('name', '')
    if not isinstance(name, str):
        return False, f"Name must be a string, got {type(name).__name__}"
    name = name.strip()
    if name:
        # Support both portable kebab-case skills and built-in display-name skills used by Hive.
        if not re.match(r'^[A-Za-z0-9][A-Za-z0-9 _-]*$', name):
            return False, (
                f"Name '{name}' contains unsupported characters. "
                "Use letters, digits, spaces, underscores, or hyphens."
            )
        if len(name) > 64:
            return False, f"Name is too long ({len(name)} characters). Maximum is 64 characters."

    # Extract and validate description
    description = frontmatter.get('description', '')
    if not isinstance(description, str):
        return False, f"Description must be a string, got {type(description).__name__}"
    description = description.strip()
    if description:
        # Check for angle brackets
        if '<' in description or '>' in description:
            return False, "Description cannot contain angle brackets (< or >)"
        # Check description length (max 1024 characters per spec)
        if len(description) > 1024:
            return False, f"Description is too long ({len(description)} characters). Maximum is 1024 characters."

    # Validate compatibility field if present (optional)
    compatibility = frontmatter.get('compatibility', '')
    if compatibility:
        if not isinstance(compatibility, str):
            return False, f"Compatibility must be a string, got {type(compatibility).__name__}"
        if len(compatibility) > 500:
            return False, f"Compatibility is too long ({len(compatibility)} characters). Maximum is 500 characters."

    tools = frontmatter.get('tools')
    if tools is not None:
        if not isinstance(tools, list) or not all(isinstance(item, str) and item.strip() for item in tools):
            return False, "tools must be a list of non-empty strings"

    allowed_tools = frontmatter.get('allowed-tools')
    if allowed_tools is not None:
        if not isinstance(allowed_tools, list) or not all(isinstance(item, str) and item.strip() for item in allowed_tools):
            return False, "allowed-tools must be a list of non-empty strings"

    is_system = frontmatter.get('is_system')
    if is_system is not None and not isinstance(is_system, bool):
        return False, f"is_system must be a boolean, got {type(is_system).__name__}"

    return True, "Skill is valid!"

if __name__ == "__main__":
    if len(sys.argv) != 2:
        logger.info("Usage: python quick_validate.py <skill_directory>")
        sys.exit(1)

    valid, message = validate_skill(sys.argv[1])
    if valid:
        logger.info(message)
    else:
        logger.error(message)
    sys.exit(0 if valid else 1)
