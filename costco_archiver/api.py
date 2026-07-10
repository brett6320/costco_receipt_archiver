"""GraphQL client for Costco's receipts API.

Endpoint and query are reverse-engineered from the web app's own network calls.
Costco may change these; the query text is centralized here and can be overridden
with the COSTCO_RECEIPTS_QUERY env var if the schema shifts.
"""
from __future__ import annotations

import os
from typing import Any

import httpx

from . import config
from .auth import Credentials

# Full receipt query. Returns warehouse + gas receipts (with line items) for a
# date window. Field set matches what the site requests today.
RECEIPTS_QUERY = os.environ.get(
    "COSTCO_RECEIPTS_QUERY",
    """
query receiptsWithCounts($startDate: String!, $endDate: String!, $documentType: String!) {
  receiptsWithCounts(startDate: $startDate, endDate: $endDate, documentType: $documentType) {
    inWarehouse
    gasStation
    carWash
    gasAndCarWash
    receipts {
      warehouseName
      warehouseShortName
      warehouseNumber
      documentType
      transactionDateTime
      transactionDate
      transactionType
      transactionBarcode
      total
      subTotal
      taxes
      totalItemCount
      instantSavings
      itemArray {
        itemNumber
        itemDescription01
        itemDescription02
        itemIdentifier
        itemDepartmentNumber
        unit
        amount
        taxFlag
        merchantID
        entryMethod
        transDepartmentNumber
        fuelGradeCode
        itemUnitPriceAmount
      }
      tenderArray {
        tenderTypeCode
        tenderDescription
        amountTender
      }
    }
  }
}
""".strip(),
)


class CostcoAPI:
    def __init__(
        self,
        creds: Credentials,
        timeout: float = 60.0,
        headers: dict | None = None,
    ):
        # Prefer exact headers captured from the browser; else reconstruct.
        hdrs = headers or self._load_saved_headers() or creds.headers()
        self._client = httpx.Client(headers=hdrs, timeout=timeout, http2=True)

    @staticmethod
    def _load_saved_headers() -> dict | None:
        import json
        from .auth import token_is_expired
        f = config.API_HEADERS_FILE
        if not f.exists():
            return None
        try:
            hdrs = json.loads(f.read_text())
        except Exception:
            return None
        # Ignore captured headers whose embedded token has expired (~15 min),
        # so a fresh env/cached token isn't shadowed by stale headers.
        auth = hdrs.get("costco-x-authorization") or hdrs.get("authorization") or ""
        tok = auth.replace("Bearer ", "").strip()
        if tok and token_is_expired(tok):
            return None
        return hdrs

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "CostcoAPI":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def receipts(
        self, start_date: str, end_date: str, document_type: str = "all"
    ) -> list[dict[str, Any]]:
        """Fetch receipts in [start_date, end_date] (YYYY-MM-DD, inclusive)."""
        payload = {
            "query": RECEIPTS_QUERY,
            "variables": {
                "startDate": start_date,
                "endDate": end_date,
                "documentType": document_type,
            },
        }
        resp = self._client.post(config.GRAPHQL_URL, json=payload)
        resp.raise_for_status()
        body = resp.json()
        if body.get("errors"):
            raise CostcoAPIError(body["errors"], body.get("data"))
        data = (body.get("data") or {}).get("receiptsWithCounts") or {}
        return data.get("receipts") or []


class CostcoAPIError(RuntimeError):
    def __init__(self, errors, data=None):
        self.errors = errors
        self.data = data
        super().__init__(f"GraphQL errors: {errors}")
