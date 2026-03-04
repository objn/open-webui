import logging
import time
from typing import Optional, List

from sqlalchemy import BigInteger, Column, ForeignKey, Text, JSON, UniqueConstraint
from sqlalchemy.orm import Session

from open_webui.internal.db import Base, get_db_context
from pydantic import BaseModel, ConfigDict

log = logging.getLogger(__name__)


class BigQueryFile(Base):
    __tablename__ = "bigquery_file"

    id = Column(Text, primary_key=True, unique=True)

    file_id = Column(
        Text, ForeignKey("file.id", ondelete="CASCADE"), nullable=False, unique=True
    )

    project = Column(Text, nullable=False)
    dataset = Column(Text, nullable=False)
    table_id = Column(Text, nullable=False)

    schema = Column(JSON, nullable=True)
    row_count = Column(BigInteger, nullable=True)

    created_at = Column(BigInteger, nullable=False)
    updated_at = Column(BigInteger, nullable=False)


class BigQueryFileModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    file_id: str

    project: str
    dataset: str
    table_id: str

    schema: Optional[dict] = None
    row_count: Optional[int] = None

    created_at: int
    updated_at: int


class BigQueryFilesTable:
    def _to_model(self, row: BigQueryFile) -> BigQueryFileModel:
        return BigQueryFileModel.model_validate(row)

    def get_by_file_id(
        self, file_id: str, db: Optional[Session] = None
    ) -> Optional[BigQueryFileModel]:
        with get_db_context(db) as db:
            try:
                row = db.query(BigQueryFile).filter_by(file_id=file_id).first()
                return self._to_model(row) if row else None
            except Exception as e:
                log.exception(f"Error getting BigQueryFile by file_id: {e}")
                return None

    def get_by_id(
        self, id: str, db: Optional[Session] = None
    ) -> Optional[BigQueryFileModel]:
        with get_db_context(db) as db:
            try:
                row = db.get(BigQueryFile, id)
                return self._to_model(row) if row else None
            except Exception as e:
                log.exception(f"Error getting BigQueryFile by id: {e}")
                return None

    def upsert_for_file(
        self,
        file_id: str,
        project: str,
        dataset: str,
        table_id: str,
        schema: Optional[dict] = None,
        row_count: Optional[int] = None,
        db: Optional[Session] = None,
    ) -> Optional[BigQueryFileModel]:
        with get_db_context(db) as db:
            try:
                now = int(time.time())
                existing = db.query(BigQueryFile).filter_by(file_id=file_id).first()

                if existing:
                    existing.project = project
                    existing.dataset = dataset
                    existing.table_id = table_id
                    if schema is not None:
                        existing.schema = schema
                    if row_count is not None:
                        existing.row_count = row_count
                    existing.updated_at = now
                    db.commit()
                    db.refresh(existing)
                    return self._to_model(existing)

                # Insert new row
                new_row = BigQueryFile(
                    id=file_id,
                    file_id=file_id,
                    project=project,
                    dataset=dataset,
                    table_id=table_id,
                    schema=schema,
                    row_count=row_count,
                    created_at=now,
                    updated_at=now,
                )
                db.add(new_row)
                db.commit()
                db.refresh(new_row)
                return self._to_model(new_row)
            except Exception as e:
                log.exception(f"Error upserting BigQueryFile: {e}")
                return None


BigQueryFiles = BigQueryFilesTable()

