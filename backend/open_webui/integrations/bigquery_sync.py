"""
BigQuery sync for file, knowledge, knowledge_file, chat_file.
Syncs rows to BigQuery when BIGQUERY_ENABLED is True (for GCP integration / dataset).
Uses MERGE for upsert-by-primary-key. Runs in fire-and-forget; errors are logged.
"""
import json
import logging
import os
import random
import string
from typing import Any, Optional

from open_webui.config import (
    BIGQUERY_DATASET,
    BIGQUERY_ENABLED,
    BIGQUERY_PROJECT,
    BIGQUERY_IMPORT_GCS_ENABLED,
    BIGQUERY_GCS_IMPORT_BUCKET,
    BIGQUERY_GCS_IMPORT_PREFIX,
    GCS_BUCKET_NAME,
    GEMINI_API_KEY,
)

log = logging.getLogger(__name__)

_client: Optional[Any] = None


def _row_to_dict(row: Any) -> dict:
    """Convert BigQuery Row to dict."""
    if hasattr(row, "keys") and hasattr(row, "values"):
        return dict(zip(row.keys(), row.values()))
    return dict(row) if not isinstance(row, dict) else row


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

    # file: id, user_id, hash, filename, path, path_volume, mime_type, ...
    file_schema = [
        SchemaField("id", "STRING", mode="REQUIRED"),
        SchemaField("user_id", "STRING", mode="NULLABLE"),
        SchemaField("hash", "STRING", mode="NULLABLE"),
        SchemaField("filename", "STRING", mode="NULLABLE"),
        SchemaField("path", "STRING", mode="NULLABLE"),
        SchemaField("path_volume", "STRING", mode="NULLABLE"),
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


def _generate_temp_table_id() -> str:
    alphabet = string.ascii_letters + string.digits
    suffix = "".join(random.choices(alphabet, k=16))
    return f"bq_temp_{suffix}"


def _get_gcs_import_bucket():
    """Return bucket name for BigQuery import via GCS, or None if not configured."""
    if BIGQUERY_GCS_IMPORT_BUCKET:
        return BIGQUERY_GCS_IMPORT_BUCKET
    return GCS_BUCKET_NAME or None


def import_structured_file_to_bigquery(local_path: str, table_id: Optional[str] = None) -> Optional[dict]:
    """Import a local structured file (CSV or Excel) into a temporary BigQuery table.

    Excel (xlsx, xls) is converted to CSV. CSV is validated for BigQuery (UTF-8,
    valid column names, consistent row length). When BIGQUERY_IMPORT_GCS_ENABLED=true,
    the prepared CSV is uploaded to GCS and BigQuery loads from gs:// (faster).
    Returns a dict with project, dataset, table_id, row_count, and schema (list of column defs),
    or None if BigQuery is disabled or the client is not available.
    """
    from open_webui.integrations.bigquery_structured import prepare_structured_file_for_bigquery

    client = _get_client()
    if client is None:
        return None

    path_to_load, cleanup_path, prep_error = prepare_structured_file_for_bigquery(local_path)
    if prep_error:
        raise ValueError(prep_error)

    use_gcs = BIGQUERY_IMPORT_GCS_ENABLED and _get_gcs_import_bucket()

    try:
        from google.cloud import bigquery

        _ensure_dataset_and_tables(client)

        if table_id is None:
            table_id = _generate_temp_table_id()

        dataset_ref = f"{BIGQUERY_PROJECT}.{BIGQUERY_DATASET}"
        full_table_id = f"{dataset_ref}.{table_id}"

        job_config = bigquery.LoadJobConfig(
            autodetect=True,
            source_format=bigquery.SourceFormat.CSV,
            skip_leading_rows=1,
        )

        if use_gcs:
            from google.cloud import storage as gcs_storage

            bucket_name = _get_gcs_import_bucket()
            gcs_client = gcs_storage.Client(project=BIGQUERY_PROJECT)
            bucket = gcs_client.bucket(bucket_name)
            blob_key = (
                f"{BIGQUERY_GCS_IMPORT_PREFIX}/{table_id}.csv"
                if BIGQUERY_GCS_IMPORT_PREFIX
                else f"{table_id}.csv"
            )
            blob = bucket.blob(blob_key)
            blob.upload_from_filename(path_to_load, content_type="text/csv")
            source_uri = f"gs://{bucket_name}/{blob_key}"
            load_job = client.load_table_from_uri(
                source_uri, full_table_id, job_config=job_config
            )
        else:
            with open(path_to_load, "rb") as f:
                load_job = client.load_table_from_file(
                    f, full_table_id, job_config=job_config
                )

        load_job.result()

        table = client.get_table(full_table_id)
        schema = [
            {"name": field.name, "type": field.field_type, "mode": field.mode}
            for field in table.schema
        ]
        return {
            "project": BIGQUERY_PROJECT,
            "dataset": BIGQUERY_DATASET,
            "table_id": table_id,
            "row_count": table.num_rows,
            "schema": schema,
        }
    except Exception as e:
        log.exception("BigQuery import_structured_file_to_bigquery failed: %s", e)
        raise
    finally:
        if cleanup_path and os.path.exists(cleanup_path):
            try:
                os.unlink(cleanup_path)
            except OSError as e:
                log.debug("Could not remove temp file %s: %s", cleanup_path, e)


def generate_bq_summary_with_gemini(
    project: str, dataset: str, table_id: str, sample_size: int = 10
) -> Optional[str]:
    """
    Query BigQuery for a sample of rows from the table, then use Gemini to generate
    a short summary: what the information likely relates to and what it contains.
    Returns the summary text, or None if disabled/failed.
    """
    if not GEMINI_API_KEY or not GEMINI_API_KEY.strip():
        log.debug("GEMINI_API_KEY not set; skipping BQ summary")
        return None

    client = _get_client()
    if client is None:
        return None

    try:
        from google.cloud import bigquery

        full_table_id = f"`{project}.{dataset}.{table_id}`"
        query = f"SELECT * FROM {full_table_id} LIMIT {sample_size}"
        query_job = client.query(query)
        rows = list(query_job.result())

        if not rows:
            return None

        # Serialize sample rows to a readable string for the model
        sample_data = []
        for row in rows:
            d = _row_to_dict(row)
            sample_data.append(d)
        sample_str = json.dumps(sample_data, default=str, indent=0)

        prompt = f"""Below is a sample of up to {sample_size} rows from a table (e.g. from an uploaded CSV).

Sample data (JSON):
{sample_str}

Answer in 1–3 short sentences: What does this information likely relate to, and what does it contain?"""

        from google import genai

        genai_client = genai.Client(api_key=GEMINI_API_KEY.strip())
        response = genai_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
        )
        if response and response.text:
            return response.text.strip()
        return None
    except Exception as e:
        log.exception("BigQuery Gemini summary failed: %s", e)
        return None


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
        path_val = row.get("path") or ""
        # Volume path: same as path but without /uploads/ segment (for GCS/volume layout).
        path_volume_val = path_val.replace("/uploads/", "/", 1) if "/uploads/" in path_val else path_val
        query = f"""
        MERGE `{dataset_ref}.file` T
        USING (SELECT @id AS id, @user_id AS user_id, @p_hash AS `hash`, @filename AS filename,
               @path AS path, @path_volume AS path_volume, @mime_type AS mime_type, @file_size AS file_size, @is_structured AS is_structured,
               @data AS data, @meta AS meta, @created_at AS created_at, @updated_at AS updated_at) S
        ON T.id = S.id
        WHEN MATCHED THEN UPDATE SET user_id=S.user_id, `hash`=S.`hash`, filename=S.filename, path=S.path, path_volume=S.path_volume,
            mime_type=S.mime_type, file_size=S.file_size, is_structured=S.is_structured,
            data=S.data, meta=S.meta, updated_at=S.updated_at
        WHEN NOT MATCHED THEN INSERT (id, user_id, `hash`, filename, path, path_volume, mime_type, file_size, is_structured, data, meta, created_at, updated_at)
            VALUES (S.id, S.user_id, S.`hash`, S.filename, S.path, S.path_volume, S.mime_type, S.file_size, S.is_structured, S.data, S.meta, S.created_at, S.updated_at)
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("id", "STRING", row.get("id") or ""),
                bigquery.ScalarQueryParameter("user_id", "STRING", row.get("user_id") or ""),
                bigquery.ScalarQueryParameter("p_hash", "STRING", row.get("hash") or ""),
                bigquery.ScalarQueryParameter("filename", "STRING", row.get("filename") or ""),
                bigquery.ScalarQueryParameter("path", "STRING", path_val),
                bigquery.ScalarQueryParameter("path_volume", "STRING", path_volume_val),
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


def _row_to_file_model(row: dict) -> "FileModel":
    from open_webui.models.files import FileModel

    data = row.get("data")
    if isinstance(data, str) and data:
        try:
            data = json.loads(data)
        except Exception:
            data = {}
    meta = row.get("meta")
    if isinstance(meta, str) and meta:
        try:
            meta = json.loads(meta)
        except Exception:
            meta = {}
    return FileModel(
        id=row.get("id") or "",
        user_id=row.get("user_id") or "",
        hash=row.get("hash"),
        filename=row.get("filename") or "",
        path=row.get("path"),
        mime_type=row.get("mime_type"),
        file_size=int(row["file_size"]) if row.get("file_size") is not None else None,
        is_structured=bool(row["is_structured"]) if row.get("is_structured") is not None else None,
        data=data,
        meta=meta,
        created_at=int(row["created_at"]) if row.get("created_at") is not None else None,
        updated_at=int(row["updated_at"]) if row.get("updated_at") is not None else None,
    )


def get_file_by_id_bq(file_id: str) -> Optional[Any]:
    """Read one file row from BigQuery by id. Returns FileModel or None."""
    client = _get_client()
    if client is None:
        return None
    try:
        _ensure_dataset_and_tables(client)
        dataset_ref = f"{BIGQUERY_PROJECT}.{BIGQUERY_DATASET}"
        query = f"SELECT * FROM `{dataset_ref}.file` WHERE id = @id LIMIT 1"
        from google.cloud import bigquery

        job_config = bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("id", "STRING", file_id)]
        )
        rows = list(client.query(query, job_config=job_config).result())
        if not rows:
            return None
        row = _row_to_dict(rows[0])
        return _row_to_file_model(row)
    except Exception as e:
        log.exception("BigQuery get_file_by_id failed: %s", e)
        return None


def get_files_by_user_id_bq(user_id: str) -> list:
    """Read file rows from BigQuery by user_id."""
    client = _get_client()
    if client is None:
        return []
    try:
        _ensure_dataset_and_tables(client)
        dataset_ref = f"{BIGQUERY_PROJECT}.{BIGQUERY_DATASET}"
        query = f"SELECT * FROM `{dataset_ref}.file` WHERE user_id = @uid ORDER BY updated_at DESC"
        from google.cloud import bigquery

        job_config = bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("uid", "STRING", user_id)]
        )
        return [_row_to_file_model(_row_to_dict(r)) for r in client.query(query, job_config=job_config).result()]
    except Exception as e:
        log.exception("BigQuery get_files_by_user_id failed: %s", e)
        return []


def get_files_by_ids_bq(ids: list[str]) -> list:
    """Read file rows from BigQuery by list of ids."""
    if not ids:
        return []
    client = _get_client()
    if client is None:
        return []
    try:
        _ensure_dataset_and_tables(client)
        dataset_ref = f"{BIGQUERY_PROJECT}.{BIGQUERY_DATASET}"
        placeholders = ", ".join(f"@id{i}" for i in range(len(ids)))
        query = f"SELECT * FROM `{dataset_ref}.file` WHERE id IN ({placeholders}) ORDER BY updated_at DESC"
        from google.cloud import bigquery

        params = [bigquery.ScalarQueryParameter(f"id{i}", "STRING", sid) for i, sid in enumerate(ids)]
        job_config = bigquery.QueryJobConfig(query_parameters=params)
        return [_row_to_file_model(_row_to_dict(r)) for r in client.query(query, job_config=job_config).result()]
    except Exception as e:
        log.exception("BigQuery get_files_by_ids failed: %s", e)
        return []


def get_all_files_bq() -> list:
    """Read all file rows from BigQuery."""
    client = _get_client()
    if client is None:
        return []
    try:
        _ensure_dataset_and_tables(client)
        dataset_ref = f"{BIGQUERY_PROJECT}.{BIGQUERY_DATASET}"
        query = f"SELECT * FROM `{dataset_ref}.file` ORDER BY updated_at DESC"
        return [_row_to_file_model(_row_to_dict(r)) for r in client.query(query).result()]
    except Exception as e:
        log.exception("BigQuery get_all_files failed: %s", e)
        return []


def update_file_data_by_id_bq(file_id: str, data: dict) -> Optional[Any]:
    """Update file.data in BigQuery; merge with existing."""
    existing = get_file_by_id_bq(file_id)
    if not existing:
        return None
    import time

    new_data = {**(existing.data or {}), **data}
    from open_webui.models.files import FileModel

    updated = FileModel(
        **{**existing.model_dump(), "data": new_data, "updated_at": int(time.time())}
    )
    sync_file(updated)
    return get_file_by_id_bq(file_id)


def update_file_metadata_by_id_bq(file_id: str, meta: dict) -> Optional[Any]:
    existing = get_file_by_id_bq(file_id)
    if not existing:
        return None
    import time

    new_meta = {**(existing.meta or {}), **meta}
    from open_webui.models.files import FileModel

    updated = FileModel(
        **{**existing.model_dump(), "meta": new_meta, "updated_at": int(time.time())}
    )
    sync_file(updated)
    return get_file_by_id_bq(file_id)


def update_file_hash_by_id_bq(file_id: str, hash_val: Optional[str]) -> Optional[Any]:
    existing = get_file_by_id_bq(file_id)
    if not existing:
        return None
    import time

    from open_webui.models.files import FileModel

    updated = FileModel(
        **{**existing.model_dump(), "hash": hash_val, "updated_at": int(time.time())}
    )
    sync_file(updated)
    return get_file_by_id_bq(file_id)


def delete_file_by_id_bq(file_id: str) -> bool:
    """Delete one file row from BigQuery."""
    client = _get_client()
    if client is None:
        return False
    try:
        dataset_ref = f"{BIGQUERY_PROJECT}.{BIGQUERY_DATASET}"
        query = f"DELETE FROM `{dataset_ref}.file` WHERE id = @id"
        from google.cloud import bigquery

        job_config = bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("id", "STRING", file_id)]
        )
        client.query(query, job_config=job_config).result()
        return True
    except Exception as e:
        log.exception("BigQuery delete_file_by_id failed: %s", e)
        return False


def insert_file_bq(user_id: str, form_data: Any) -> Optional[Any]:
    """Insert file into BigQuery only (no PostgreSQL). Returns FileModel."""
    import time

    from open_webui.models.files import FileModel

    fd = form_data.model_dump() if hasattr(form_data, "model_dump") else form_data
    file = FileModel(
        **{
            **fd,
            "user_id": user_id,
            "created_at": int(time.time()),
            "updated_at": int(time.time()),
        }
    )
    sync_file(file)
    return get_file_by_id_bq(file.id)


def get_file_metadata_by_id_bq(file_id: str) -> Optional[Any]:
    """Return FileMetadataResponse from BigQuery."""
    from open_webui.models.files import FileMetadataResponse

    f = get_file_by_id_bq(file_id)
    if not f:
        return None
    return FileMetadataResponse(
        id=f.id,
        hash=f.hash,
        meta=f.meta,
        created_at=f.created_at or 0,
        updated_at=f.updated_at or 0,
    )


def get_file_metadatas_by_ids_bq(ids: list[str]) -> list:
    """Return list of FileMetadataResponse from BigQuery."""
    from open_webui.models.files import FileMetadataResponse

    files = get_files_by_ids_bq(ids)
    return [
        FileMetadataResponse(
            id=f.id,
            hash=f.hash,
            meta=f.meta,
            created_at=f.created_at or 0,
            updated_at=f.updated_at or 0,
        )
        for f in files
    ]


def search_files_bq(
    user_id: Optional[str] = None,
    filename: str = "*",
    skip: int = 0,
    limit: int = 100,
) -> list:
    """Search files in BigQuery. filename supports * and ? as glob."""
    client = _get_client()
    if client is None:
        return []
    try:
        _ensure_dataset_and_tables(client)
        dataset_ref = f"{BIGQUERY_PROJECT}.{BIGQUERY_DATASET}"
        conditions = []
        params = []
        from google.cloud import bigquery

        if user_id:
            conditions.append("user_id = @user_id")
            params.append(bigquery.ScalarQueryParameter("user_id", "STRING", user_id))
        if filename and filename != "*":
            # Glob to LIKE: * -> %, ? -> _, escape % _ \
            like = filename.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_").replace("*", "%").replace("?", "_")
            conditions.append("LOWER(filename) LIKE LOWER(@pattern)")
            params.append(bigquery.ScalarQueryParameter("pattern", "STRING", like))
        where = " AND ".join(conditions) if conditions else "1=1"
        query = f"SELECT * FROM `{dataset_ref}.file` WHERE {where} ORDER BY updated_at DESC LIMIT @lim OFFSET @off"
        params.extend([
            bigquery.ScalarQueryParameter("lim", "INT64", limit),
            bigquery.ScalarQueryParameter("off", "INT64", skip),
        ])
        job_config = bigquery.QueryJobConfig(query_parameters=params)
        return [_row_to_file_model(_row_to_dict(r)) for r in client.query(query, job_config=job_config).result()]
    except Exception as e:
        log.exception("BigQuery search_files failed: %s", e)
        return []


def delete_all_files_bq() -> bool:
    """Delete all file rows from BigQuery."""
    client = _get_client()
    if client is None:
        return False
    try:
        dataset_ref = f"{BIGQUERY_PROJECT}.{BIGQUERY_DATASET}"
        query = f"DELETE FROM `{dataset_ref}.file` WHERE TRUE"
        client.query(query).result()
        return True
    except Exception as e:
        log.exception("BigQuery delete_all_files failed: %s", e)
        return False


def update_file_by_id_bq(file_id: str, form_data: Any) -> Optional[Any]:
    """Update file by id in BigQuery (hash, data, meta)."""
    existing = get_file_by_id_bq(file_id)
    if not existing:
        return None
    import time

    from open_webui.models.files import FileModel

    upd = existing.model_dump()
    if form_data.hash is not None:
        upd["hash"] = form_data.hash
    if form_data.data is not None:
        upd["data"] = {**(existing.data or {}), **form_data.data}
    if form_data.meta is not None:
        upd["meta"] = {**(existing.meta or {}), **form_data.meta}
    upd["updated_at"] = int(time.time())
    updated = FileModel(**upd)
    sync_file(updated)
    return get_file_by_id_bq(file_id)


# ---------- Knowledge ----------
def _row_to_knowledge_model(row: dict) -> Any:
    from open_webui.models.knowledge import KnowledgeModel

    meta = row.get("meta")
    if isinstance(meta, str) and meta:
        try:
            meta = json.loads(meta)
        except Exception:
            meta = {}
    return KnowledgeModel(
        id=row.get("id") or "",
        user_id=row.get("user_id") or "",
        name=row.get("name") or "",
        description=row.get("description") or "",
        meta=meta,
        created_at=int(row["created_at"]) if row.get("created_at") is not None else 0,
        updated_at=int(row["updated_at"]) if row.get("updated_at") is not None else 0,
    )


def get_knowledge_by_id_bq(knowledge_id: str) -> Optional[Any]:
    client = _get_client()
    if client is None:
        return None
    try:
        _ensure_dataset_and_tables(client)
        dataset_ref = f"{BIGQUERY_PROJECT}.{BIGQUERY_DATASET}"
        from google.cloud import bigquery

        query = f"SELECT * FROM `{dataset_ref}.knowledge` WHERE id = @id LIMIT 1"
        job_config = bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("id", "STRING", knowledge_id)]
        )
        rows = list(client.query(query, job_config=job_config).result())
        if not rows:
            return None
        return _row_to_knowledge_model(_row_to_dict(rows[0]))
    except Exception as e:
        log.exception("BigQuery get_knowledge_by_id failed: %s", e)
        return None


def insert_knowledge_bq(user_id: str, form_data: Any) -> Optional[Any]:
    import time
    import uuid
    from open_webui.models.knowledge import KnowledgeModel

    k = KnowledgeModel(
        **{
            **form_data.model_dump(exclude={"access_grants"}, exclude_none=True),
            "id": str(uuid.uuid4()),
            "user_id": user_id,
            "created_at": int(time.time()),
            "updated_at": int(time.time()),
        }
    )
    sync_knowledge(k)
    return get_knowledge_by_id_bq(k.id)


def update_knowledge_by_id_bq(knowledge_id: str, form_data: Any) -> Optional[Any]:
    existing = get_knowledge_by_id_bq(knowledge_id)
    if not existing:
        return None
    import time
    from open_webui.models.knowledge import KnowledgeModel

    upd = existing.model_dump()
    for k in ("name", "description", "meta"):
        if hasattr(form_data, k) and getattr(form_data, k) is not None:
            upd[k] = getattr(form_data, k)
    upd["updated_at"] = int(time.time())
    sync_knowledge(KnowledgeModel(**upd))
    return get_knowledge_by_id_bq(knowledge_id)


def update_knowledge_data_by_id_bq(knowledge_id: str, data: dict) -> Optional[Any]:
    """Update knowledge updated_at only (BQ knowledge table has no data column). Returns refreshed knowledge."""
    client = _get_client()
    if client is None:
        return None
    try:
        import time

        dataset_ref = f"{BIGQUERY_PROJECT}.{BIGQUERY_DATASET}"
        from google.cloud import bigquery

        query = f"UPDATE `{dataset_ref}.knowledge` SET updated_at = @ts WHERE id = @id"
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("ts", "INT64", int(time.time())),
                bigquery.ScalarQueryParameter("id", "STRING", knowledge_id),
            ]
        )
        client.query(query, job_config=job_config).result()
        return get_knowledge_by_id_bq(knowledge_id)
    except Exception as e:
        log.exception("BigQuery update_knowledge_data_by_id failed: %s", e)
        return None


def get_knowledge_files_by_knowledge_id_bq(knowledge_id: str) -> list:
    client = _get_client()
    if client is None:
        return []
    try:
        _ensure_dataset_and_tables(client)
        dataset_ref = f"{BIGQUERY_PROJECT}.{BIGQUERY_DATASET}"
        from google.cloud import bigquery
        from open_webui.models.knowledge import KnowledgeFileModel

        query = f"SELECT * FROM `{dataset_ref}.knowledge_file` WHERE knowledge_id = @kid ORDER BY created_at ASC"
        job_config = bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("kid", "STRING", knowledge_id)]
        )
        return [
            KnowledgeFileModel(
                id=str(r.id),
                knowledge_id=str(r.knowledge_id),
                file_id=str(r.file_id),
                user_id=str(r.user_id),
                created_at=int(r.created_at) if r.created_at is not None else 0,
                updated_at=int(r.updated_at) if r.updated_at is not None else 0,
            )
            for r in client.query(query, job_config=job_config).result()
        ]
    except Exception as e:
        log.exception("BigQuery get_knowledge_files_by_knowledge_id failed: %s", e)
        return []


def add_knowledge_file_bq(knowledge_id: str, file_id: str, user_id: str) -> Optional[Any]:
    import time
    import uuid
    from open_webui.models.knowledge import KnowledgeFileModel

    kf = KnowledgeFileModel(
        id=str(uuid.uuid4()),
        knowledge_id=knowledge_id,
        file_id=file_id,
        user_id=user_id,
        created_at=int(time.time()),
        updated_at=int(time.time()),
    )
    sync_knowledge_file(kf)
    return kf


def remove_knowledge_file_bq(knowledge_id: str, file_id: str) -> bool:
    client = _get_client()
    if client is None:
        return False
    try:
        dataset_ref = f"{BIGQUERY_PROJECT}.{BIGQUERY_DATASET}"
        from google.cloud import bigquery

        query = f"DELETE FROM `{dataset_ref}.knowledge_file` WHERE knowledge_id = @kid AND file_id = @fid"
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("kid", "STRING", knowledge_id),
                bigquery.ScalarQueryParameter("fid", "STRING", file_id),
            ]
        )
        client.query(query, job_config=job_config).result()
        return True
    except Exception as e:
        log.exception("BigQuery remove_knowledge_file failed: %s", e)
        return False


def get_knowledges_by_file_id_bq(file_id: str) -> list:
    client = _get_client()
    if client is None:
        return []
    try:
        _ensure_dataset_and_tables(client)
        dataset_ref = f"{BIGQUERY_PROJECT}.{BIGQUERY_DATASET}"
        from google.cloud import bigquery

        query = f"SELECT k.* FROM `{dataset_ref}.knowledge` k INNER JOIN `{dataset_ref}.knowledge_file` kf ON k.id = kf.knowledge_id WHERE kf.file_id = @fid"
        job_config = bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("fid", "STRING", file_id)]
        )
        return [_row_to_knowledge_model(_row_to_dict(r)) for r in client.query(query, job_config=job_config).result()]
    except Exception as e:
        log.exception("BigQuery get_knowledges_by_file_id failed: %s", e)
        return []


def delete_knowledge_by_id_bq(knowledge_id: str) -> bool:
    client = _get_client()
    if client is None:
        return False
    try:
        dataset_ref = f"{BIGQUERY_PROJECT}.{BIGQUERY_DATASET}"
        from google.cloud import bigquery

        client.query(f"DELETE FROM `{dataset_ref}.knowledge_file` WHERE knowledge_id = @kid", job_config=bigquery.QueryJobConfig(query_parameters=[bigquery.ScalarQueryParameter("kid", "STRING", knowledge_id)])).result()
        client.query(f"DELETE FROM `{dataset_ref}.knowledge` WHERE id = @id", job_config=bigquery.QueryJobConfig(query_parameters=[bigquery.ScalarQueryParameter("id", "STRING", knowledge_id)])).result()
        return True
    except Exception as e:
        log.exception("BigQuery delete_knowledge_by_id failed: %s", e)
        return False


def get_all_knowledges_bq() -> list:
    client = _get_client()
    if client is None:
        return []
    try:
        _ensure_dataset_and_tables(client)
        dataset_ref = f"{BIGQUERY_PROJECT}.{BIGQUERY_DATASET}"
        query = f"SELECT * FROM `{dataset_ref}.knowledge` ORDER BY updated_at DESC"
        return [_row_to_knowledge_model(_row_to_dict(r)) for r in client.query(query).result()]
    except Exception as e:
        log.exception("BigQuery get_all_knowledges failed: %s", e)
        return []


def reset_knowledge_by_id_bq(knowledge_id: str) -> bool:
    """Delete all knowledge_file rows for this knowledge and update knowledge.updated_at."""
    client = _get_client()
    if client is None:
        return False
    try:
        import time

        dataset_ref = f"{BIGQUERY_PROJECT}.{BIGQUERY_DATASET}"
        from google.cloud import bigquery

        client.query(
            f"DELETE FROM `{dataset_ref}.knowledge_file` WHERE knowledge_id = @kid",
            job_config=bigquery.QueryJobConfig(
                query_parameters=[bigquery.ScalarQueryParameter("kid", "STRING", knowledge_id)]
            ),
        ).result()
        client.query(
            f"UPDATE `{dataset_ref}.knowledge` SET updated_at = @ts WHERE id = @id",
            job_config=bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("ts", "INT64", int(time.time())),
                    bigquery.ScalarQueryParameter("id", "STRING", knowledge_id),
                ]
            ),
        ).result()
        return True
    except Exception as e:
        log.exception("BigQuery reset_knowledge_by_id failed: %s", e)
        return False


def delete_all_knowledge_bq() -> bool:
    client = _get_client()
    if client is None:
        return False
    try:
        dataset_ref = f"{BIGQUERY_PROJECT}.{BIGQUERY_DATASET}"
        from google.cloud import bigquery

        client.query(f"DELETE FROM `{dataset_ref}.knowledge_file` WHERE TRUE").result()
        client.query(f"DELETE FROM `{dataset_ref}.knowledge` WHERE TRUE").result()
        return True
    except Exception as e:
        log.exception("BigQuery delete_all_knowledge failed: %s", e)
        return False


# ---------- ChatFile ----------
def _row_to_chat_file_model(row: dict) -> Any:
    from open_webui.models.chats import ChatFileModel

    return ChatFileModel(
        id=row.get("id") or "",
        user_id=row.get("user_id") or "",
        chat_id=row.get("chat_id") or "",
        message_id=row.get("message_id"),
        file_id=row.get("file_id") or "",
        created_at=int(row["created_at"]) if row.get("created_at") is not None else 0,
        updated_at=int(row["updated_at"]) if row.get("updated_at") is not None else 0,
    )


def insert_chat_files_bq(chat_id: str, message_id: str, file_ids: list[str], user_id: str) -> Optional[list]:
    import time
    import uuid
    from open_webui.models.chats import ChatFileModel

    created = []
    for file_id in file_ids:
        if not file_id:
            continue
        cf = ChatFileModel(
            id=str(uuid.uuid4()),
            user_id=user_id,
            chat_id=chat_id,
            message_id=message_id,
            file_id=file_id,
            created_at=int(time.time()),
            updated_at=int(time.time()),
        )
        sync_chat_file(cf)
        created.append(cf)
    return created if created else None


def get_chat_files_by_chat_id_and_message_id_bq(chat_id: str, message_id: str) -> list:
    client = _get_client()
    if client is None:
        return []
    try:
        _ensure_dataset_and_tables(client)
        dataset_ref = f"{BIGQUERY_PROJECT}.{BIGQUERY_DATASET}"
        from google.cloud import bigquery

        query = f"SELECT * FROM `{dataset_ref}.chat_file` WHERE chat_id = @cid AND message_id = @mid ORDER BY created_at ASC"
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("cid", "STRING", chat_id),
                bigquery.ScalarQueryParameter("mid", "STRING", message_id or ""),
            ]
        )
        rows = list(client.query(query, job_config=job_config).result())
        return [_row_to_chat_file_model(_row_to_dict(r)) for r in rows]
    except Exception as e:
        log.exception("BigQuery get_chat_files_by_chat_id_and_message_id failed: %s", e)
        return []


def delete_chat_file_bq(chat_id: str, file_id: str) -> bool:
    client = _get_client()
    if client is None:
        return False
    try:
        dataset_ref = f"{BIGQUERY_PROJECT}.{BIGQUERY_DATASET}"
        from google.cloud import bigquery

        query = f"DELETE FROM `{dataset_ref}.chat_file` WHERE chat_id = @cid AND file_id = @fid"
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("cid", "STRING", chat_id),
                bigquery.ScalarQueryParameter("fid", "STRING", file_id),
            ]
        )
        client.query(query, job_config=job_config).result()
        return True
    except Exception as e:
        log.exception("BigQuery delete_chat_file failed: %s", e)
        return False


def get_chat_ids_by_file_id_bq(file_id: str) -> list:
    """Return list of chat_id that reference this file_id (for get_shared_chats)."""
    client = _get_client()
    if client is None:
        return []
    try:
        _ensure_dataset_and_tables(client)
        dataset_ref = f"{BIGQUERY_PROJECT}.{BIGQUERY_DATASET}"
        from google.cloud import bigquery

        query = f"SELECT DISTINCT chat_id FROM `{dataset_ref}.chat_file` WHERE file_id = @fid"
        job_config = bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("fid", "STRING", file_id)]
        )
        return [str(r.chat_id) for r in client.query(query, job_config=job_config).result()]
    except Exception as e:
        log.exception("BigQuery get_chat_ids_by_file_id failed: %s", e)
        return []
