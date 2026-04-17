"""
GET /triggers/vocabulary — returns the controlled trigger vocabulary.
UI uses this for autocomplete/suggestions.
"""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from trigger_vocab import CANONICAL_TRIGGERS, ALIASES

router = APIRouter(prefix="/triggers", tags=["triggers"])


class TriggerVocabularyResponse(BaseModel):
    triggers: list[str]
    aliases: dict[str, str]


@router.get("/vocabulary", response_model=TriggerVocabularyResponse)
async def get_vocabulary():
    return TriggerVocabularyResponse(
        triggers=sorted(CANONICAL_TRIGGERS),
        aliases=dict(ALIASES),
    )
