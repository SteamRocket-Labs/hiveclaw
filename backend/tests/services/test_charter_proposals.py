"""Phase 15: charter calibration approval surface tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.services.charter_proposals import (
    CharterProposalStore,
    ProposalAlreadyDecided,
    ProposalKind,
)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "charter_proposals.db"


@pytest.fixture
def store(db_path: Path) -> CharterProposalStore:
    return CharterProposalStore(db_path)


def _submit_default(store: CharterProposalStore) -> str:
    proposal = store.submit(
        agent_id="agent-1",
        decision_id="decision/dec-1",
        action="send_feishu_message",
        proposal_kind=ProposalKind.CONSIDER_FULL_AUTHORITY,
        reason="Owner approved an external vendor reply twice in confirm_first",
    )
    return proposal.id


class TestSubmitAndList:
    def test_submit_creates_pending_proposal(self, store: CharterProposalStore) -> None:
        proposal = store.submit(
            agent_id="agent-1",
            decision_id="decision/dec-1",
            action="send_feishu_message",
            proposal_kind=ProposalKind.TIGHTEN_TO_CONFIRM_FIRST,
            reason="Owner rejected a full-authority send",
        )
        assert proposal.status == "pending"
        assert proposal.decided_at is None
        assert proposal.decided_by is None

    def test_list_pending_only_returns_open_proposals(self, store: CharterProposalStore) -> None:
        proposal_id = _submit_default(store)
        store.approve(proposal_id, by="alice")
        assert store.list_pending() == []

    def test_list_pending_filters_by_agent(self, store: CharterProposalStore) -> None:
        _submit_default(store)
        store.submit(
            agent_id="agent-2",
            decision_id="decision/dec-2",
            action="post_to_plaza",
            proposal_kind=ProposalKind.CONSIDER_FULL_AUTHORITY,
            reason="agent-2 has cleared confirm_first 3x",
        )
        pending_for_agent_1 = store.list_pending(agent_id="agent-1")
        pending_for_agent_2 = store.list_pending(agent_id="agent-2")
        assert len(pending_for_agent_1) == 1
        assert len(pending_for_agent_2) == 1
        assert pending_for_agent_1[0].agent_id == "agent-1"


class TestApproveReject:
    def test_approve_sets_status_and_metadata(self, store: CharterProposalStore) -> None:
        proposal_id = _submit_default(store)
        approved = store.approve(proposal_id, by="alice", decision_reason="LGTM")
        assert approved.status == "approved"
        assert approved.decided_by == "alice"
        assert approved.decided_at is not None
        assert approved.decision_reason == "LGTM"

    def test_reject_sets_status_and_metadata(self, store: CharterProposalStore) -> None:
        proposal_id = _submit_default(store)
        rejected = store.reject(proposal_id, by="alice", decision_reason="Still risky")
        assert rejected.status == "rejected"
        assert rejected.decision_reason == "Still risky"

    def test_double_decision_raises(self, store: CharterProposalStore) -> None:
        proposal_id = _submit_default(store)
        store.approve(proposal_id, by="alice")
        with pytest.raises(ProposalAlreadyDecided):
            store.approve(proposal_id, by="alice")
        with pytest.raises(ProposalAlreadyDecided):
            store.reject(proposal_id, by="alice")


class TestExpire:
    def test_expire_stale_marks_old_pending_as_expired(self, store: CharterProposalStore) -> None:
        old_time = datetime.now(timezone.utc) - timedelta(days=14)
        proposal = store.submit(
            agent_id="agent-1",
            decision_id="decision/old",
            action="send_email",
            proposal_kind=ProposalKind.CONSIDER_FULL_AUTHORITY,
            reason="stale",
            created_at=old_time,
        )
        recent_id = _submit_default(store)
        expired_ids = store.expire_stale(max_age_days=7)
        assert proposal.id in expired_ids
        assert recent_id not in expired_ids
        refreshed = store.get(proposal.id)
        assert refreshed is not None
        assert refreshed.status == "expired"


class TestReload:
    def test_proposals_survive_store_reopen(self, db_path: Path) -> None:
        first = CharterProposalStore(db_path)
        proposal_id = _submit_default(first)
        first.approve(proposal_id, by="alice")

        second = CharterProposalStore(db_path)
        loaded = second.get(proposal_id)
        assert loaded is not None
        assert loaded.status == "approved"
        assert loaded.decided_by == "alice"


class TestUnknown:
    def test_get_unknown_returns_none(self, store: CharterProposalStore) -> None:
        assert store.get("missing") is None

    def test_approve_unknown_raises(self, store: CharterProposalStore) -> None:
        with pytest.raises(KeyError):
            store.approve("missing", by="alice")
