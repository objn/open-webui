"""
Prepare structured files (CSV, Excel) for BigQuery load.
- Converts Excel (xlsx, xls) to CSV.
- Validates and sanitizes CSV for BigQuery compatibility (UTF-8, column names, row consistency).
"""
import csv
import io
import logging
import os
import re
import tempfile
import uuid
from typing import Optional, Tuple

log = logging.getLogger(__name__)

# BigQuery column names: letters, digits, underscore only (case-sensitive)
_BQ_COLUMN_NAME_PATTERN = re.compile(r"[^a-zA-Z0-9_]")
# Structured extensions we support for BigQuery import
EXCEL_EXTENSIONS = frozenset({"xlsx", "xls"})
CSV_EXTENSIONS = frozenset({"csv"})
STRUCTURED_EXTENSIONS_FOR_BQ = CSV_EXTENSIONS | EXCEL_EXTENSIONS

# Reasonable limits to avoid runaway load
MAX_CSV_ROWS = 10_000_000
MAX_CSV_COLUMNS = 10_000


def _extension(path: str) -> str:
    ext = os.path.splitext(path)[1]
    return (ext[1:] if ext.startswith(".") else ext).lower()


def _sanitize_bigquery_column_name(name: str) -> str:
    """Make a string valid as a BigQuery column name (letters, digits, underscore)."""
    if not name or not name.strip():
        return "column"
    s = _BQ_COLUMN_NAME_PATTERN.sub("_", name.strip())
    if s[0].isdigit():
        s = "col_" + s
    return s[:300] or "column"


def _ensure_utf8(content: bytes) -> Tuple[str, Optional[str]]:
    """Decode content as UTF-8; try common fallbacks on failure. Returns (decoded_str, error)."""
    try:
        return content.decode("utf-8"), None
    except UnicodeDecodeError:
        pass
    for enc in ("utf-8-sig", "cp874", "cp1252", "latin-1"):
        try:
            return content.decode(enc), None
        except (UnicodeDecodeError, LookupError):
            continue
    return "", "CSV encoding is not supported. Use UTF-8."


def _validate_and_sanitize_csv(
    csv_path: str,
    max_rows: int = MAX_CSV_ROWS,
    max_columns: int = MAX_CSV_COLUMNS,
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Validate CSV and optionally write a sanitized version for BigQuery.
    Returns (path_to_use, cleanup_path, error_message).
    - path_to_use: path to CSV to load (original or temp file).
    - cleanup_path: temp file to delete after load, or None.
    - error_message: if set, path_to_use and cleanup_path are None.
    """
    try:
        with open(csv_path, "rb") as f:
            raw = f.read()
    except OSError as e:
        return None, None, f"Cannot read file: {e}"

    text, enc_error = _ensure_utf8(raw)
    if enc_error:
        return None, None, enc_error

    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if not lines:
        return None, None, "CSV file is empty."

    # Parse header and rows
    reader = csv.reader(io.StringIO(text))
    try:
        header_row = next(reader)
    except StopIteration:
        return None, None, "CSV file has no header row."

    if not header_row:
        return None, None, "CSV header row is empty."

    num_cols = len(header_row)
    if num_cols > max_columns:
        return None, None, f"Too many columns (max {max_columns})."

    sanitized_headers = [_sanitize_bigquery_column_name(h) for h in header_row]
    seen = set()
    for i, h in enumerate(sanitized_headers):
        if h in seen:
            suffix = 1
            while f"{h}_{suffix}" in seen:
                suffix += 1
            sanitized_headers[i] = f"{h}_{suffix}"
        seen.add(sanitized_headers[i])

    row_count = 0
    rows_ok = []
    for row in reader:
        row_count += 1
        if row_count > max_rows:
            return None, None, f"Too many rows (max {max_rows})."
        if len(row) != num_cols:
            return None, None, (
                f"Row {row_count + 1} has {len(row)} columns, expected {num_cols}. "
                "All rows must have the same number of columns."
            )
        rows_ok.append(row)

    # If we only changed headers (sanitized), write temp file for BigQuery
    original_headers = header_row
    needs_rewrite = any(a != b for a, b in zip(original_headers, sanitized_headers))

    if not needs_rewrite:
        return csv_path, None, None

    fd, temp_path = tempfile.mkstemp(suffix=".csv", prefix="bq_")
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(sanitized_headers)
            w.writerows(rows_ok)
        return temp_path, temp_path, None
    except OSError as e:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        return None, None, f"Cannot write sanitized CSV: {e}"


def excel_to_csv(excel_path: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Convert Excel file (xlsx, xls) to CSV. Returns (csv_temp_path, error_message).
    Caller must delete csv_temp_path when done.
    """
    import pandas as pd

    ext = _extension(excel_path)
    if ext not in EXCEL_EXTENSIONS:
        return None, f"Unsupported Excel extension: .{ext}"

    try:
        if ext == "xlsx":
            df = pd.read_excel(excel_path, engine="openpyxl")
        else:
            df = pd.read_excel(excel_path, engine="xlrd")
    except Exception as e:
        log.exception("Excel read failed: %s", e)
        return None, f"Could not read Excel file: {e}"

    if df.empty and len(df.columns) == 0:
        return None, "Excel sheet is empty."

    fd, temp_path = tempfile.mkstemp(suffix=".csv", prefix="bq_excel_")
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
            df.to_csv(f, index=False, date_format="%Y-%m-%d %H:%M:%S")
        return temp_path, None
    except OSError as e:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        return None, f"Cannot write CSV: {e}"


def prepare_structured_file_for_bigquery(
    local_path: str,
    max_rows: int = MAX_CSV_ROWS,
    max_columns: int = MAX_CSV_COLUMNS,
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Prepare a structured file (CSV or Excel) for BigQuery load.
    Returns (path_to_load, cleanup_path, error_message).
    - path_to_load: path to a CSV file to pass to load_table_from_file.
    - cleanup_path: temp file to delete after load (if any); otherwise None.
    - error_message: if set, the other two are None.
    """
    if not os.path.isfile(local_path):
        return None, None, "File not found."

    ext = _extension(local_path)
    if ext not in STRUCTURED_EXTENSIONS_FOR_BQ:
        return None, None, f"Unsupported format for BigQuery: .{ext}"

    csv_path = local_path
    excel_cleanup: Optional[str] = None

    if ext in EXCEL_EXTENSIONS:
        csv_path, excel_err = excel_to_csv(local_path)
        if excel_err:
            return None, None, excel_err
        excel_cleanup = csv_path

    path_to_use, validate_cleanup, err = _validate_and_sanitize_csv(
        csv_path, max_rows=max_rows, max_columns=max_columns
    )
    if err:
        if excel_cleanup:
            try:
                os.unlink(excel_cleanup)
            except OSError:
                pass
        return None, None, err

    cleanup = excel_cleanup or validate_cleanup
    return path_to_use, cleanup, None
