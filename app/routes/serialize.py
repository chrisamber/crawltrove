"""DB-row -> camelCase JSON serializers for the jobs/runs API.

The JSON API is camelCase (matching the rest of the service); the database and
Python are snake_case. Timestamps are emitted as ISO-8601 strings.
"""
import datetime
from typing import Any, Dict, Optional


def _iso(value: Any) -> Optional[str]:
    if isinstance(value, datetime.datetime):
        return value.isoformat()
    return value


def job_to_api(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": row.get("id"),
        "name": row.get("name"),
        "kind": row.get("kind"),
        "targetUrl": row.get("target_url"),
        "params": row.get("params") or {},
        "schedule": row.get("schedule"),
        "enabled": row.get("enabled"),
        "lastRunAt": _iso(row.get("last_run_at")),
        "nextRunAt": _iso(row.get("next_run_at")),
        "createdAt": _iso(row.get("created_at")),
        "updatedAt": _iso(row.get("updated_at")),
    }


def run_to_api(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": row.get("id"),
        "externalId": row.get("external_id"),
        "jobId": row.get("job_id"),
        "trigger": row.get("trigger"),
        "status": row.get("status"),
        "engineUsed": row.get("engine_used"),
        "pagesCount": row.get("pages_count"),
        "errorMessage": row.get("error_message"),
        "rawOutputPath": row.get("raw_output_path"),
        "startedAt": _iso(row.get("started_at")),
        "finishedAt": _iso(row.get("finished_at")),
        "createdAt": _iso(row.get("created_at")),
    }


def record_to_api(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": row.get("id"),
        "pageId": row.get("page_id"),
        "sourceUrl": row.get("source_url"),
        "recordType": row.get("record_type"),
        "data": row.get("data_json"),
        "contentHash": row.get("content_hash"),
        "confidence": row.get("confidence"),
        "createdAt": _iso(row.get("created_at")),
    }


def search_hit_to_api(row: Dict[str, Any]) -> Dict[str, Any]:
    meta = row.get("metadata") or {}
    lic = meta.get("license")
    return {
        "id": row.get("id"),
        "url": row.get("url"),
        "runId": row.get("run_id"),
        "contentHash": row.get("content_hash"),
        "title": meta.get("title"),
        "language": meta.get("language"),
        "license": lic.get("id") if isinstance(lic, dict) else lic,
        "rank": row.get("rank"),
        "snippet": row.get("snippet"),
    }


def page_to_api(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": row.get("id"),
        "url": row.get("url"),
        "statusCode": row.get("status_code"),
        "engine": row.get("engine"),
        "extractor": row.get("extractor"),
        "contentHash": row.get("content_hash"),
        "rawJsonPath": row.get("raw_json_path"),
        "rawMdPath": row.get("raw_md_path"),
        "rawHtmlPath": row.get("raw_html_path"),
        "metadata": row.get("metadata"),
        "createdAt": _iso(row.get("created_at")),
    }
