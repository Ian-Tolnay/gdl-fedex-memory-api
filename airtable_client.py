from __future__ import annotations

import os
import time
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import quote

import httpx


class AirtableError(RuntimeError):
    pass


class AirtableClient:
    def __init__(self, token: Optional[str] = None, base_id: Optional[str] = None):
        self.token = token or os.getenv("AIRTABLE_TOKEN")
        self.base_id = base_id or os.getenv("AIRTABLE_BASE_ID")
        if not self.token or not self.base_id:
            raise AirtableError("AIRTABLE_TOKEN and AIRTABLE_BASE_ID must be set")
        self.base_url = f"https://api.airtable.com/v0/{self.base_id}"
        self.headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

    def _url(self, table: str) -> str:
        return f"{self.base_url}/{quote(table, safe='')}"

    def list_records(
        self,
        table: str,
        formula: Optional[str] = None,
        max_records: int = 100,
        page_size: int = 100,
        view: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        records: List[Dict[str, Any]] = []
        offset: Optional[str] = None
        with httpx.Client(timeout=25) as client:
            while True:
                params: Dict[str, Any] = {"pageSize": min(page_size, 100)}
                if formula:
                    params["filterByFormula"] = formula
                if view:
                    params["view"] = view
                if offset:
                    params["offset"] = offset
                resp = client.get(self._url(table), headers=self.headers, params=params)
                if resp.status_code == 429:
                    time.sleep(30)
                    continue
                if resp.status_code >= 400:
                    raise AirtableError(f"Airtable list failed {resp.status_code}: {resp.text}")
                data = resp.json()
                records.extend(data.get("records", []))
                if len(records) >= max_records:
                    return records[:max_records]
                offset = data.get("offset")
                if not offset:
                    return records

    def create_records(self, table: str, fields_list: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
        fields = list(fields_list)
        if not fields:
            return []
        created: List[Dict[str, Any]] = []
        with httpx.Client(timeout=30) as client:
            for i in range(0, len(fields), 10):
                batch = fields[i : i + 10]
                payload = {"records": [{"fields": self._clean_fields(row)} for row in batch]}
                while True:
                    resp = client.post(self._url(table), headers=self.headers, json=payload)
                    if resp.status_code == 429:
                        time.sleep(30)
                        continue
                    if resp.status_code >= 400:
                        raise AirtableError(f"Airtable create failed {resp.status_code}: {resp.text}")
                    created.extend(resp.json().get("records", []))
                    break
                time.sleep(0.22)  # stay under 5 req/sec/base
        return created

    def update_record(self, table: str, record_id: str, fields: Dict[str, Any]) -> Dict[str, Any]:
        payload = {"fields": self._clean_fields(fields)}
        with httpx.Client(timeout=25) as client:
            resp = client.patch(f"{self._url(table)}/{record_id}", headers=self.headers, json=payload)
            if resp.status_code >= 400:
                raise AirtableError(f"Airtable update failed {resp.status_code}: {resp.text}")
            return resp.json()

    @staticmethod
    def _clean_fields(fields: Dict[str, Any]) -> Dict[str, Any]:
        cleaned: Dict[str, Any] = {}
        for key, value in fields.items():
            if value is None:
                continue
            if isinstance(value, list):
                # Airtable linked records and multi-selects are both lists. We store most list fields as CSV/text except Tags.
                if key in {"tags", "Tags"}:
                    cleaned[key] = value
                else:
                    cleaned[key] = ", ".join(str(v) for v in value)
            else:
                cleaned[key] = value
        return cleaned


def airtable_formula_equals(field_name: str, value: str) -> str:
    safe_value = value.replace("'", "\\'")
    return f"{{{field_name}}}='{safe_value}'"
