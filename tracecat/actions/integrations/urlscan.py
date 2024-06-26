import os
from typing import Any, Literal

import httpx
from tenacity import retry, stop_after_delay, wait_combine, wait_fixed

from tracecat.registry import registry

URLSCAN_BASE_URL = "https://urlscan.io/api/v1/"


def create_urlscan_client() -> httpx.AsyncClient:
    headers = {"API-Key": os.environ["URLSCAN_API_KEY"]}
    return httpx.AsyncClient(base_url=URLSCAN_BASE_URL, headers=headers)


@retry(wait=wait_combine(wait_fixed(2), wait_fixed(10)), stop=stop_after_delay(120))
async def get_scan_result(scan_id: str) -> dict[str, Any]:
    async with create_urlscan_client() as client:
        rsp = await client.get(f"result/{scan_id}/")
        if rsp.status_code == 200:
            return rsp.json()
        else:
            rsp.raise_for_status()


@registry.register(
    namespace="urlscan",
    description="Analyze a URL and get report. Private scan by default.",
    secrets=["urlscan"],
)
async def analyze_url(
    url: str, visibility: Literal["public", "unlisted", "private"] = "private"
) -> dict[str, Any]:
    """Analyze a URL and get report."""
    async with create_urlscan_client() as client:
        # Submit the URL for scanning
        rsp = await client.post("scan/", json={"url": url, "visibility": visibility})
        rsp.raise_for_status()
        scan_id = rsp.json().get("uuid")
        if scan_id is None:
            raise ValueError("Scan ID not found in response")
        # Wait for scan results
        return get_scan_result(scan_id)
