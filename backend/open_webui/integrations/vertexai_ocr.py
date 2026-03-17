import mimetypes
import os
from typing import Optional, Tuple

from loguru import logger as log


def _parse_gs_uri(gs_uri: str) -> Tuple[str, str]:
    if not isinstance(gs_uri, str) or not gs_uri.startswith("gs://"):
        raise ValueError("Expected a GCS URI starting with gs://")
    after = gs_uri.removeprefix("gs://")
    parts = after.split("/", 1)
    bucket = parts[0].strip()
    if not bucket:
        raise ValueError("Invalid GCS URI (missing bucket)")
    blob = parts[1] if len(parts) > 1 else ""
    blob = blob.lstrip("/")
    if not blob:
        raise ValueError("Invalid GCS URI (missing object path)")
    return bucket, blob


def _guess_mime_type(filename_or_uri: str) -> str:
    mt, _ = mimetypes.guess_type(filename_or_uri)
    return mt or "application/octet-stream"


def _download_gcs_bytes(gs_uri: str) -> Tuple[bytes, str]:
    """
    Download GCS object to memory (bytes). Uses ADC/service account.
    Returns (data, mime_type).
    """
    try:
        from google.cloud import storage
    except Exception as e:
        raise RuntimeError(f"google-cloud-storage not available: {e}") from e

    bucket_name, blob_name = _parse_gs_uri(gs_uri)
    client = storage.Client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(blob_name)
    data = blob.download_as_bytes()
    mime_type = blob.content_type or _guess_mime_type(blob_name)
    return data, mime_type


def _vertexai_ocr_from_bytes(
    data: bytes,
    *,
    mime_type: str,
    project: Optional[str] = None,
    location: Optional[str] = None,
) -> str:
    try:
        import vertexai
        from vertexai.generative_models import GenerationConfig, GenerativeModel, Part
    except Exception as e:
        raise RuntimeError(f"Vertex AI SDK not available: {e}") from e

    resolved_project = (
        project
        or os.environ.get("VERTEXAI_PROJECT")
        or os.environ.get("GOOGLE_CLOUD_PROJECT")
        or ""
    ).strip()
    if not resolved_project:
        raise RuntimeError(
            "Missing GCP project. Set VERTEXAI_PROJECT or GOOGLE_CLOUD_PROJECT, or pass project=..."
        )
    resolved_location = (location or os.environ.get("VERTEXAI_LOCATION") or "asia-southeast1").strip()

    vertexai.init(project=resolved_project, location=resolved_location)
    model = GenerativeModel("gemini-2.5-flash")

    prompt = (
        "You are an OCR engine. Extract ALL readable text from the provided document/image. "
        "Preserve reading order and line breaks as much as possible. "
        "Return only the extracted text (no markdown fences, no commentary)."
    )

    try:
        response = model.generate_content(
            [
                prompt,
                Part.from_data(data=data, mime_type=mime_type),
            ],
            generation_config=GenerationConfig(temperature=0),
        )
        text = getattr(response, "text", None)
        return text.strip() if text else ""
    except Exception as e:
        log.exception("Vertex AI OCR failed")
        raise RuntimeError(f"Vertex AI OCR failed: {e}") from e


def vertexai_ocr_from_path(
    file_path: str,
    *,
    project: Optional[str] = None,
    location: Optional[str] = None,
) -> str:
    """
    Load a local file and extract text using Vertex AI Gemini 2.5 Flash.

    - Auth: Application Default Credentials (or service account in environment)
    - Model: gemini-2.5-flash
    """
    if not isinstance(file_path, str) or not file_path.strip():
        raise ValueError("Expected a local file path")

    try:
        with open(file_path, "rb") as f:
            data = f.read()
    except Exception as e:
        raise RuntimeError(f"Failed to read file for OCR: {e}") from e

    mime_type = _guess_mime_type(file_path)
    return _vertexai_ocr_from_bytes(
        data,
        mime_type=mime_type,
        project=project,
        location=location,
    )


def vertexai_ocr(
    file_uri_gcs: str,
    *,
    project: Optional[str] = None,
    location: Optional[str] = None,
) -> str:
    """
    Load a file from GCS (gs://...) and extract text using Vertex AI Gemini 2.5 Flash.

    - Auth: Application Default Credentials (or service account in environment)
    - Model: gemini-2.5-flash
    """
    data, mime_type = _download_gcs_bytes(file_uri_gcs)
    try:
        return _vertexai_ocr_from_bytes(
            data,
            mime_type=mime_type,
            project=project,
            location=location,
        )
    except Exception as e:
        log.exception("Vertex AI OCR failed for %s", file_uri_gcs)
        raise


def vertexai_ocr_from_openwebui_path(
    file_path: str,
    *,
    project: Optional[str] = None,
    location: Optional[str] = None,
    data_dir: Optional[str] = None,
    gcs_bucket: Optional[str] = None,
    gcs_key_prefix: Optional[str] = None,
) -> str:
    """
    OCR helper that understands OpenWebUI-style paths.

    Accepts:
    - Absolute local paths under DATA_DIR (e.g. /app/backend/data/uploads/unstructured/...)
    - Logical OpenWebUI keys (e.g. uploads/unstructured/...)
    - GCS URIs (gs://bucket/prefix/uploads/...)

    Resolution order:
    - If input is gs://... -> OCR from GCS
    - Else, if it can derive a gs:// from uploads/... + bucket (+ prefix) -> OCR from GCS
    - Else -> OCR from local path
    """
    if not isinstance(file_path, str) or not file_path.strip():
        raise ValueError("Expected a file path or gs:// URI")

    p = file_path.strip()
    if p.startswith("gs://"):
        return vertexai_ocr(p, project=project, location=location)

    # Normalize absolute DATA_DIR paths to logical "uploads/..." keys
    if data_dir:
        dd = os.fspath(data_dir).rstrip("/\\")
        if dd and os.path.isabs(p) and p.startswith(dd + "/"):
            p = p[len(dd) + 1 :]

    # Derive gs:// from logical uploads/... key if bucket available
    if p.startswith("uploads/") and gcs_bucket:
        key = p
        if gcs_key_prefix:
            key = f"{gcs_key_prefix.rstrip('/')}/{key.lstrip('/')}"
        gs_uri = f"gs://{gcs_bucket}/{key.lstrip('/')}"
        return vertexai_ocr(gs_uri, project=project, location=location)

    # Fall back to local path OCR
    return vertexai_ocr_from_path(p, project=project, location=location)