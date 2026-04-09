"""AnswerAccumulator — manages the full answer array for the CLINICAL_TREE loop.

HAR-confirmed rules:
  - FULL accumulated array sent on every API call (never just the new answer)
  - Backtrack calls DeleteAssetsByGroupId for all downstream groups first
  - Must be JSON-serializable for Restate journal persistence
"""
from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


class AnswerAccumulator:
    """Accumulates answers for the clinical question loop.

    Each answer is keyed by GroupId. The payload property returns the FULL
    sorted array on every access — this is critical for the portal API.
    """

    def __init__(self, initial: list[dict] | None = None):
        # {group_id: answer_dict}
        self._answers: dict[int, dict] = {}
        if initial:
            for answer in initial:
                gid = answer.get("GroupId")
                if gid is None:
                    gid = answer.get("group_id")
                if gid is not None:
                    self._answers[int(gid)] = answer

    def add(self, answer: dict) -> None:
        """Add or replace answer for a GroupId."""
        gid = answer.get("GroupId")
        if gid is None:
            gid = answer.get("group_id")
        if gid is None:
            raise ValueError("Answer must have a GroupId")
        self._answers[int(gid)] = answer
        logger.info(f"AnswerAccumulator: added GroupId={gid}, total={self.count}")

    async def change(
        self,
        answer: dict,
        session: Any,
        delete_endpoint: str,
    ) -> None:
        """Backtrack: delete all downstream groups, then add new answer.

        Per HAR Rule 4: call DeleteAssetsByGroupId for every GroupId > changed GroupId,
        then remove downstream from accumulator, resume from changed GroupId.
        """
        gid = answer.get("GroupId")
        if gid is None:
            gid = answer.get("group_id")
        if gid is None:
            raise ValueError("Answer must have a GroupId")
        gid = int(gid)

        # Find all groups downstream of the changed group
        downstream = sorted(g for g in self._answers if g > gid)

        # Delete downstream groups from portal (most recent first)
        for downstream_gid in reversed(downstream):
            logger.info(f"AnswerAccumulator: backtrack deleting GroupId={downstream_gid}")
            await session.api(delete_endpoint, {"groupId": downstream_gid})
            del self._answers[downstream_gid]

        # Now add/replace the changed answer
        self._answers[gid] = answer
        logger.info(
            f"AnswerAccumulator: backtrack complete, changed GroupId={gid}, "
            f"deleted {len(downstream)} downstream, total={self.count}"
        )

    def get_new_groups(self, response_questions: list[dict]) -> list[dict]:
        """Return questions from the response that are not yet in the accumulator."""
        new = []
        for q in response_questions:
            gid = q.get("GroupId")
            if gid is None:
                gid = q.get("group_id")
            if gid is not None and int(gid) not in self._answers:
                new.append(q)
        return new

    def has_group(self, group_id: int) -> bool:
        """Check if a GroupId has already been answered."""
        return int(group_id) in self._answers

    @property
    def payload(self) -> list[dict]:
        """Full sorted answer array for API submission.

        ALWAYS returns the complete accumulated history — never just new answers.
        """
        return [self._answers[gid] for gid in sorted(self._answers)]

    @property
    def count(self) -> int:
        return len(self._answers)

    # --- Serialization (Restate journal persistence) ---

    def to_json(self) -> str:
        return json.dumps(self.payload)

    @classmethod
    def from_json(cls, data: str) -> AnswerAccumulator:
        answers = json.loads(data)
        return cls(initial=answers)

    def to_dict(self) -> list[dict]:
        return self.payload

    @classmethod
    def from_dict(cls, data: list[dict]) -> AnswerAccumulator:
        return cls(initial=data)
