"""Load, persist, and apply administrator tuning as a Settings overlay."""

from dataclasses import replace

from sqlalchemy.orm import Session

from app.config import Settings
from app.models.tuning import KnowledgeTuningSetting
from app.schemas.tuning import KnowledgeTuningRead, KnowledgeTuningValues


TUNING_SETTING_ID = 1
TUNING_FIELDS = tuple(KnowledgeTuningValues.model_fields)


def tuning_values_from_settings(settings: Settings) -> KnowledgeTuningValues:
    return KnowledgeTuningValues.model_validate(
        {field: getattr(settings, field) for field in TUNING_FIELDS}
    )


def effective_settings(session: Session, base: Settings) -> Settings:
    row = session.get(KnowledgeTuningSetting, TUNING_SETTING_ID)
    if row is None:
        return base
    values = KnowledgeTuningValues.model_validate(row.values_json)
    return replace(base, **values.model_dump())


class KnowledgeTuningService:
    def __init__(self, session: Session, base_settings: Settings) -> None:
        self.session = session
        self.base_settings = base_settings

    def get(self) -> KnowledgeTuningRead:
        row = self.session.get(KnowledgeTuningSetting, TUNING_SETTING_ID)
        values = (
            KnowledgeTuningValues.model_validate(row.values_json)
            if row is not None
            else tuning_values_from_settings(self.base_settings)
        )
        return KnowledgeTuningRead(
            **values.model_dump(),
            updated_at=row.updated_at if row is not None else None,
        )

    def update(self, values: KnowledgeTuningValues) -> KnowledgeTuningRead:
        row = self.session.get(KnowledgeTuningSetting, TUNING_SETTING_ID)
        if row is None:
            row = KnowledgeTuningSetting(
                id=TUNING_SETTING_ID,
                values_json=values.model_dump(),
            )
            self.session.add(row)
        else:
            row.values_json = values.model_dump()
        self.session.commit()
        self.session.refresh(row)
        return KnowledgeTuningRead(
            **values.model_dump(),
            updated_at=row.updated_at,
        )

    def reset(self) -> KnowledgeTuningRead:
        row = self.session.get(KnowledgeTuningSetting, TUNING_SETTING_ID)
        if row is not None:
            self.session.delete(row)
            self.session.commit()
        values = tuning_values_from_settings(self.base_settings)
        return KnowledgeTuningRead(**values.model_dump(), updated_at=None)
