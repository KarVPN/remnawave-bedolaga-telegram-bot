from datetime import date
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from app.cabinet.schemas.users import UserStatusEnum


class UserBulkFilters(BaseModel):
    search: str | None = Field(None, max_length=255)
    email: str | None = Field(None, max_length=255)
    status: UserStatusEnum | None = None
    subscription_on_date: date | None = None
    subscription_status: Literal['active', 'trial', 'expired', 'disabled', 'limited'] | None = None
    has_subscription: bool | None = None
    has_restrictions: bool | None = None
    expires_within_days: int | None = Field(None, ge=1, le=365)


class UserBulkPreview(BaseModel):
    request_key: UUID
    action: Literal['add_traffic', 'extend_subscription', 'deduct_days']
    traffic_gb: Literal[10, 50, 100] | None = None
    days: int | None = Field(None, ge=1, le=3650)
    user_ids: list[int] | None = Field(None, min_length=1, max_length=5000)
    filters: UserBulkFilters | None = None
    selection_id: UUID | None = None
    target_filters: UserBulkFilters | None = None

    @model_validator(mode='after')
    def validate_target_and_params(self):
        if sum(value is not None for value in (self.user_ids, self.filters, self.selection_id)) != 1:
            raise ValueError('Provide user_ids or filters, exclusively')
        if self.action == 'add_traffic':
            if self.traffic_gb is None or self.days is not None:
                raise ValueError('Traffic package required; days not allowed')
        elif self.days is None or self.traffic_gb is not None:
            raise ValueError('Positive days required; traffic package not allowed')
        return self
