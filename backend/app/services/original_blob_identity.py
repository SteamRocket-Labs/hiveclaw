"""Portable exact identities for legacy and generational original objects."""

import uuid


def original_object_key(tenant_id, digest, *, generation_id=None):
    tenant = str(uuid.UUID(str(tenant_id)))
    if not isinstance(digest, str) or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ValueError("Invalid original content identity")
    suffix = "." + str(uuid.UUID(str(generation_id))) if generation_id is not None else ""
    return f"{tenant}/{digest}{suffix}"
