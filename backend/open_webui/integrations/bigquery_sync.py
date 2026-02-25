"""
BigQuery sync for file, knowledge, knowledge_file, chat_file.
Syncs rows to BigQuery when BIGQUERY_ENABLED is True (for GCP integration / dataset).
Uses MERGE for upsert-by-primary-key. Runs in fire-and-forget; errors are logged.
"""
import json
import logging
from typing import Any, Optional

from open_webui.config import (
    BIGQUERY_DATASET,
    BIGQUERY_ENABLED,
    BIGQUERY_PROJECT,
)

log = logging.getLogger(__name__)

_client: Optional[Any] = None


def _get_client():
    global _client
    if _client is not None:
        return _client
    if not BIGQUERY_ENABLED or not BIGQUERY_PROJECT or not BIGQUERY_DATASET:
        return None
    try:
        from google.cloud import bigquery

        _client = bigquery.Client(project=BIGQUERY_PROJECT)
        return _client
    except Exception as e:
        log.warning("BigQuery client init failed: %s", e)
        return None


def _ensure_dataset_and_tables(client) -> None:
    from google.cloud import bigquery
    from google.cloud.bigquery import SchemaField, Table

    dataset_ref_str = f"{BIGQUERY_PROJECT}.{BIGQUERY_DATASET}"
    try:
        client.get_dataset(dataset_ref_str)
    except Exception:
        ref = bigquery.DatasetReference(BIGQUERY_PROJECT, BIGQUERY_DATASET)
        client.create_dataset(bigquery.Dataset(ref), exists_ok=True)

    dataset_ref = dataset_ref_str

    # file: id, user_id, hash, filename, path, mime_type, file_size, is_structured, data, meta, created_at, updated_at
    file_schema = [
        SchemaField("id", "STRING", mode="REQUIRED"),
        SchemaField("user_id", "STRING", mode="NULLABLE"),
        SchemaField("hash", "STRING", mode="NULLABLE"),
        SchemaField("filename", "STRING", mode="NULLABLE"),
        SchemaField("path", "STRING", mode="NULLABLE"),
        SchemaField("mime_type", "STRING", mode="NULLABLE"),
        SchemaField("file_size", "INTEGER", mode="NULLABLE"),
        SchemaField("is_structured", "BOOL", mode="NULLABLE"),
        SchemaField("data", "STRING", mode="NULLABLE"),
        SchemaField("meta", "STRING", mode="NULLABLE"),
        SchemaField("created_at", "INTEGER", mode="NULLABLE"),
        SchemaField("updated_at", "INTEGER", mode="NULLABLE"),
    ]
    file_table_id = f"{dataset_ref_str}.file"
    try:
        existing = client.get_table(file_table_id)
        existing_fields = {f.name for f in existing.schema}
        for field in file_schema:
            if field.name not in existing_fields:
                try:
                    client.query(
                        f"ALTER TABLE `{file_table_id}` ADD COLUMN {field.name} {field.field_type}"
                    ).result()
                except Exception as alter_err:
                    log.debug("BigQuery add column %s: %s", field.name, alter_err)
    except Exception:
        client.create_table(Table(file_table_id, schema=file_schema), exists_ok=True)

    # knowledge: id, user_id, name, description, meta, created_at, updated_at
    knowledge_schema = [
        SchemaField("id", "STRING", mode="REQUIRED"),
        SchemaField("user_id", "STRING", mode="NULLABLE"),
        SchemaField("name", "STRING", mode="NULLABLE"),
        SchemaField("description", "STRING", mode="NULLABLE"),
        SchemaField("meta", "STRING", mode="NULLABLE"),
        SchemaField("created_at", "INTEGER", mode="NULLABLE"),
        SchemaField("updated_at", "INTEGER", mode="NULLABLE"),
    ]
    knowledge_table_id = f"{dataset_ref_str}.knowledge"
    try:
        client.get_table(knowledge_table_id)
    except Exception:
        client.create_table(
            Table(knowledge_table_id, schema=knowledge_schema), exists_ok=True
        )

    # knowledge_file: id, knowledge_id, file_id, user_id, created_at, updated_at
    knowledge_file_schema = [
        SchemaField("id", "STRING", mode="REQUIRED"),
        SchemaField("knowledge_id", "STRING", mode="NULLABLE"),
        SchemaField("file_id", "STRING", mode="NULLABLE"),
        SchemaField("user_id", "STRING", mode="NULLABLE"),
        SchemaField("created_at", "INTEGER", mode="NULLABLE"),
        SchemaField("updated_at", "INTEGER", mode="NULLABLE"),
    ]
    kf_table_id = f"{dataset_ref_str}.knowledge_file"
    try:
        client.get_table(kf_table_id)
    except Exception:
        client.create_table(
            Table(kf_table_id, schema=knowledge_file_schema), exists_ok=True
        )

    # chat_file: id, user_id, chat_id, message_id, file_id, created_at, updated_at
    chat_file_schema = [
        SchemaField("id", "STRING", mode="REQUIRED"),
        SchemaField("user_id", "STRING", mode="NULLABLE"),
        SchemaField("chat_id", "STRING", mode="NULLABLE"),
        SchemaField("message_id", "STRING", mode="NULLABLE"),
        SchemaField("file_id", "STRING", mode="NULLABLE"),
        SchemaField("created_at", "INTEGER", mode="NULLABLE"),
        SchemaField("updated_at", "INTEGER", mode="NULLABLE"),
    ]
    cf_table_id = f"{dataset_ref_str}.chat_file"
    try:
        client.get_table(cf_table_id)
    except Exception:
        client.create_table(
            Table(cf_table_id, schema=chat_file_schema), exists_ok=True
        )


def sync_file(file_item: Any) -> None:
    """Upsert one file row into BigQuery. Fire-and-forget; errors logged."""
    client = _get_client()
    if client is None:
        return
    try:
        from google.cloud import bigquery

        _ensure_dataset_and_tables(client)
        dataset_ref = f"{BIGQUERY_PROJECT}.{BIGQUERY_DATASET}"
        fd = file_item if hasattr(file_item, "model_dump") else file_item
        row = fd.model_dump() if hasattr(fd, "model_dump") else fd
        data_str = json.dumps(row.get("data") or {})
        meta_str = json.dumps(row.get("meta") or {})
        mime_type = row.get("mime_type") or ""
        file_size = row.get("file_size")
        if file_size is None:
            file_size = 0
        is_structured = row.get("is_structured")
        if is_structured is None:
            is_structured = False
        query = f"""
        MERGE `{dataset_ref}.file` T
        USING (SELECT @id AS id, @user_id AS user_id, @hash AS hash, @filename AS filename,
               @path AS path, @mime_type AS mime_type, @file_size AS file_size, @is_structured AS is_structured,
               @data AS data, @meta AS meta, @created_at AS created_at, @updated_at AS updated_at) S
        ON T.id = S.id
        WHEN MATCHED THEN UPDATE SET user_id=S.user_id, hash=S.hash, filename=S.filename, path=S.path,
            mime_type=S.mime_type, file_size=S.file_size, is_structured=S.is_structured,
            data=S.data, meta=S.meta, updated_at=S.updated_at
        WHEN NOT MATCHED THEN INSERT (id, user_id, hash, filename, path, mime_type, file_size, is_structured, data, meta, created_at, updated_at)
            VALUES (S.id, S.user_id, S.hash, S.filename, S.path, S.mime_type, S.file_size, S.is_structured, S.data, S.meta, S.created_at, S.updated_at)
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("id", "STRING", row.get("id") or ""),
                bigquery.ScalarQueryParameter("user_id", "STRING", row.get("user_id") or ""),
                bigquery.ScalarQueryParameter("hash", "STRING", row.get("hash") or ""),
                bigquery.ScalarQueryParameter("filename", "STRING", row.get("filename") or ""),
                bigquery.ScalarQueryParameter("path", "STRING", row.get("path") or ""),
                bigquery.ScalarQueryParameter("mime_type", "STRING", mime_type),
                bigquery.ScalarQueryParameter("file_size", "INT64", file_size),
                bigquery.ScalarQueryParameter("is_structured", "BOOL", is_structured),
                bigquery.ScalarQueryParameter("data", "STRING", data_str),
                bigquery.ScalarQueryParameter("meta", "STRING", meta_str),
                bigquery.ScalarQueryParameter("created_at", "INT64", row.get("created_at") or 0),
                bigquery.ScalarQueryParameter("updated_at", "INT64", row.get("updated_at") or 0),
            ]
        )
        client.query(query, job_config=job_config).result()
    except Exception as e:
        log.exception("BigQuery sync_file failed: %s", e)


def sync_knowledge(knowledge_item: Any) -> None:
    """Upsert one knowledge row into BigQuery."""
    client = _get_client()
    if client is None:
        return
    try:
        from google.cloud import bigquery

        _ensure_dataset_and_tables(client)
        dataset_ref = f"{BIGQUERY_PROJECT}.{BIGQUERY_DATASET}"
        row = (
            knowledge_item.model_dump()
            if hasattr(knowledge_item, "model_dump")
            else knowledge_item
        )
        meta_str = json.dumps(row.get("meta") or {})
        query = f"""
        MERGE `{dataset_ref}.knowledge` T
        USING (SELECT @id AS id, @user_id AS user_id, @name AS name, @description AS description,
               @meta AS meta, @created_at AS created_at, @updated_at AS updated_at) S
        ON T.id = S.id
        WHEN MATCHED THEN UPDATE SET user_id=S.user_id, name=S.name, description=S.description,
            meta=S.meta, updated_at=S.updated_at
        WHEN NOT MATCHED THEN INSERT (id, user_id, name, description, meta, created_at, updated_at)
            VALUES (S.id, S.user_id, S.name, S.description, S.meta, S.created_at, S.updated_at)
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("id", "STRING", row.get("id") or ""),
                bigquery.ScalarQueryParameter("user_id", "STRING", row.get("user_id") or ""),
                bigquery.ScalarQueryParameter("name", "STRING", row.get("name") or ""),
                bigquery.ScalarQueryParameter("description", "STRING", row.get("description") or ""),
                bigquery.ScalarQueryParameter("meta", "STRING", meta_str),
                bigquery.ScalarQueryParameter("created_at", "INT64", row.get("created_at") or 0),
                bigquery.ScalarQueryParameter("updated_at", "INT64", row.get("updated_at") or 0),
            ]
        )
        client.query(query, job_config=job_config).result()
    except Exception as e:
        log.exception("BigQuery sync_knowledge failed: %s", e)


def sync_knowledge_file(item: Any) -> None:
    """Upsert one knowledge_file row into BigQuery."""
    client = _get_client()
    if client is None:
        return
    try:
        from google.cloud import bigquery

        _ensure_dataset_and_tables(client)
        dataset_ref = f"{BIGQUERY_PROJECT}.{BIGQUERY_DATASET}"
        row = item.model_dump() if hasattr(item, "model_dump") else item
        query = f"""
        MERGE `{dataset_ref}.knowledge_file` T
        USING (SELECT @id AS id, @knowledge_id AS knowledge_id, @file_id AS file_id, @user_id AS user_id,
               @created_at AS created_at, @updated_at AS updated_at) S
        ON T.id = S.id
        WHEN MATCHED THEN UPDATE SET knowledge_id=S.knowledge_id, file_id=S.file_id, user_id=S.user_id, updated_at=S.updated_at
        WHEN NOT MATCHED THEN INSERT (id, knowledge_id, file_id, user_id, created_at, updated_at)
            VALUES (S.id, S.knowledge_id, S.file_id, S.user_id, S.created_at, S.updated_at)
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("id", "STRING", row.get("id") or ""),
                bigquery.ScalarQueryParameter("knowledge_id", "STRING", row.get("knowledge_id") or ""),
                bigquery.ScalarQueryParameter("file_id", "STRING", row.get("file_id") or ""),
                bigquery.ScalarQueryParameter("user_id", "STRING", row.get("user_id") or ""),
                bigquery.ScalarQueryParameter("created_at", "INT64", row.get("created_at") or 0),
                bigquery.ScalarQueryParameter("updated_at", "INT64", row.get("updated_at") or 0),
            ]
        )
        client.query(query, job_config=job_config).result()
    except Exception as e:
        log.exception("BigQuery sync_knowledge_file failed: %s", e)


def sync_chat_file(item: Any) -> None:
    """Upsert one chat_file row into BigQuery."""
    client = _get_client()
    if client is None:
        return
    try:
        from google.cloud import bigquery

        _ensure_dataset_and_tables(client)
        dataset_ref = f"{BIGQUERY_PROJECT}.{BIGQUERY_DATASET}"
        row = item.model_dump() if hasattr(item, "model_dump") else item
        query = f"""
        MERGE `{dataset_ref}.chat_file` T
        USING (SELECT @id AS id, @user_id AS user_id, @chat_id AS chat_id, @message_id AS message_id,
               @file_id AS file_id, @created_at AS created_at, @updated_at AS updated_at) S
        ON T.id = S.id
        WHEN MATCHED THEN UPDATE SET user_id=S.user_id, chat_id=S.chat_id, message_id=S.message_id, file_id=S.file_id, updated_at=S.updated_at
        WHEN NOT MATCHED THEN INSERT (id, user_id, chat_id, message_id, file_id, created_at, updated_at)
            VALUES (S.id, S.user_id, S.chat_id, S.message_id, S.file_id, S.created_at, S.updated_at)
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("id", "STRING", row.get("id") or ""),
                bigquery.ScalarQueryParameter("user_id", "STRING", row.get("user_id") or ""),
                bigquery.ScalarQueryParameter("chat_id", "STRING", row.get("chat_id") or ""),
                bigquery.ScalarQueryParameter("message_id", "STRING", row.get("message_id") or ""),
                bigquery.ScalarQueryParameter("file_id", "STRING", row.get("file_id") or ""),
                bigquery.ScalarQueryParameter("created_at", "INT64", row.get("created_at") or 0),
                bigquery.ScalarQueryParameter("updated_at", "INT64", row.get("updated_at") or 0),
            ]
        )
        client.query(query, job_config=job_config).result()
    except Exception as e:
        log.exception("BigQuery sync_chat_file failed: %s", e)
