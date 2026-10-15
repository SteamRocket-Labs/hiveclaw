"""Explicit identity options for Company Knowledge user grants.

Directory people and authenticated users are different records. A grant uses
``User.id`` only. Matching uses provider subject columns, never a name, an
email address, or an unrelated external id.
"""

from __future__ import annotations

from typing import Any


def _clean(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _add(bucket: dict[str, list[Any]], key: str | None, user: Any) -> None:
    if key is None:
        return
    matches = bucket.setdefault(key, [])
    if all(str(existing.id) != str(user.id) for existing in matches):
        matches.append(user)


def _subject_indexes(users: list[Any]) -> tuple[dict[str, list[Any]], dict[str, list[Any]], dict[str, list[Any]]]:
    by_open: dict[str, list[Any]] = {}
    by_union: dict[str, list[Any]] = {}
    by_feishu_user: dict[str, list[Any]] = {}
    for user in users:
        _add(by_open, _clean(getattr(user, "feishu_open_id", None)), user)
        _add(by_union, _clean(getattr(user, "feishu_union_id", None)), user)
        _add(by_feishu_user, _clean(getattr(user, "feishu_user_id", None)), user)
    return by_open, by_union, by_feishu_user


def _matched_users(member: Any, indexes) -> list[Any]:
    by_open, by_union, by_feishu_user = indexes
    found: dict[str, Any] = {}
    subjects: list[tuple[dict[str, list[Any]], str | None]] = [
        (by_open, _clean(getattr(member, "feishu_open_id", None))),
        (by_open, _clean(getattr(member, "open_id", None))),
        (by_union, _clean(getattr(member, "unionid", None))),
        (by_feishu_user, _clean(getattr(member, "feishu_user_id", None))),
        (by_feishu_user, _clean(getattr(member, "external_id", None))),
    ]
    for bucket, key in subjects:
        if key is None:
            continue
        for user in bucket.get(key, []):
            found[str(user.id)] = user
    return list(found.values())


def company_knowledge_grant_audiences(users: list[Any], org_members: list[Any]) -> dict[str, list[dict[str, str]]]:
    """Return grantable authenticated users and directory people who are not.

    An org member linked to exactly one active user is not a second grant
    target. Ambiguous, inactive, and unregistered people stay visible and
    cannot be selected. An authenticated user who participates in an
    ambiguous directory match is not a selectable grant target either.
    """

    indexes = _subject_indexes(users)
    matches_by_member = [(member, _matched_users(member, indexes)) for member in org_members]
    ambiguous_user_ids = {
        str(user.id)
        for _member, matched in matches_by_member
        if len(matched) > 1
        for user in matched
    }
    grantable: list[dict[str, str]] = []
    for user in users:
        if getattr(user, "is_active", True) is False or str(user.id) in ambiguous_user_ids:
            continue
        label = str(getattr(user, "display_name", "") or getattr(user, "username", "") or "Authenticated user")
        grantable.append({"user_id": str(user.id), "label": label})
    grantable.sort(key=lambda item: (item["label"], item["user_id"]))

    unlinked: list[dict[str, str]] = []
    for member, matched in matches_by_member:
        if len(matched) == 1 and getattr(matched[0], "is_active", True) is not False:
            continue
        if len(matched) > 1:
            reason = "ambiguous_identity"
        elif len(matched) == 1:
            reason = "authenticated_user_inactive"
        else:
            reason = "unregistered"
        unlinked.append(
            {
                "org_member_id": str(member.id),
                "label": str(getattr(member, "name", "") or "Directory person"),
                "reason": reason,
            }
        )
    unlinked.sort(key=lambda item: (item["label"], item["org_member_id"]))
    return {"grantable_users": grantable, "visible_unlinked": unlinked}


_FIELD_KINDS = (
    ("feishu_open_id", "provider_open_id"),
    ("open_id", "provider_open_id"),
    ("unionid", "provider_union_id"),
    ("feishu_user_id", "provider_user_id"),
    ("external_id", "provider_user_id"),
)


def explicit_subject_matches(users: list[Any], member: Any) -> dict[str, Any]:
    """Return the users and subject kinds matched for one directory person.

    Uses the same subject indexes as ``company_knowledge_grant_audiences``.
    Name, email, and equal ids are not inputs.
    """

    by_open, by_union, by_feishu_user = _subject_indexes(users)
    buckets = {
        "provider_open_id": by_open,
        "provider_union_id": by_union,
        "provider_user_id": by_feishu_user,
    }
    kinds_by_user: dict[str, list[str]] = {}
    subjects_by_user: dict[str, dict[str, str]] = {}
    for field, kind in _FIELD_KINDS:
        key = _clean(getattr(member, field, None))
        if key is None:
            continue
        for user in buckets[kind].get(key, []):
            user_id = str(user.id)
            kinds = kinds_by_user.setdefault(user_id, [])
            if kind not in kinds:
                kinds.append(kind)
            subjects_by_user.setdefault(user_id, {}).setdefault(kind, key)
    return {
        "users": _matched_users(member, (by_open, by_union, by_feishu_user)),
        "kinds_by_user": kinds_by_user,
        "subjects_by_user": subjects_by_user,
    }
