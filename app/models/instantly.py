"""Pydantic v2 models for the Instantly v2 API."""

from pydantic import BaseModel


class LeadPayload(BaseModel):
    """Lead variables from lead.payload dict. Flexible — any string keys."""

    model_config = {"extra": "allow"}


class Lead(BaseModel):
    id: str
    email: str
    status: int
    payload: dict[str, str | None] = {}  # Lead variables — key is var name, value is var value


class CampaignVariant(BaseModel):
    subject: str = ""
    body: str = ""


class CampaignStep(BaseModel):
    variants: list[CampaignVariant] = []


class CampaignSequence(BaseModel):
    steps: list[CampaignStep] = []


class Campaign(BaseModel):
    id: str
    name: str
    status: int
    sequences: list[CampaignSequence] = []
