"""Minimal Shared Key client for the Azurite queue endpoint (stdlib only)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ElementTree
from email.utils import formatdate

from .resolve import AZURITE_ACCOUNT, AZURITE_KEY

API_VERSION = "2021-12-02"


class AzuriteError(RuntimeError):
    pass


class AzuriteClient:
    def __init__(self, queue_endpoint: str, account: str = AZURITE_ACCOUNT, key: str = AZURITE_KEY) -> None:
        self.queue_endpoint = queue_endpoint.rstrip("/")
        self.account = account
        self.key = base64.b64decode(key)

    def _sign(self, method: str, url: str, headers: dict[str, str], body: bytes) -> str:
        parsed = urllib.parse.urlparse(url)
        canonical_headers = "".join(
            f"{name}:{headers[name]}\n" for name in sorted(h for h in headers if h.startswith("x-ms-"))
        )
        # Path-style emulator URLs already contain the account: /devstoreaccount1/queue
        resource = f"/{self.account}{parsed.path}"
        query = urllib.parse.parse_qs(parsed.query)
        for name in sorted(query):
            resource += f"\n{name.lower()}:{','.join(sorted(query[name]))}"
        length = str(len(body)) if body else ""
        string_to_sign = "\n".join([
            method,
            "",  # Content-Encoding
            "",  # Content-Language
            length,
            "",  # Content-MD5
            headers.get("Content-Type", ""),
            "",  # Date (x-ms-date is used)
            "", "", "", "", "",  # If-*, Range
        ]) + "\n" + canonical_headers + resource
        digest = hmac.new(self.key, string_to_sign.encode("utf-8"), hashlib.sha256).digest()
        return f"SharedKey {self.account}:{base64.b64encode(digest).decode()}"

    def _call(self, method: str, url: str, body: bytes = b"", content_type: str = "") -> tuple[int, bytes, dict[str, str]]:
        headers = {"x-ms-date": formatdate(usegmt=True), "x-ms-version": API_VERSION}
        if content_type:
            headers["Content-Type"] = content_type
        headers["Authorization"] = self._sign(method, url, headers, body)
        request = urllib.request.Request(url, data=body or None, method=method, headers=headers)
        for attempt in range(3):
            try:
                with urllib.request.urlopen(request, timeout=15) as response:
                    return response.status, response.read(), dict(response.headers)
            except urllib.error.HTTPError as error:
                return error.code, error.read(), dict(error.headers)
            except (urllib.error.URLError, ConnectionError) as error:
                # Published ports reset idle connections after the Mac wakes from sleep.
                if attempt == 2:
                    reason = getattr(error, "reason", error)
                    raise AzuriteError(f"{method} {url}: {reason}") from error
                time.sleep(1 + attempt)
        raise AzuriteError(f"{method} {url}: no response")

    def _request(self, method: str, url: str, body: bytes = b"", content_type: str = "") -> tuple[int, bytes]:
        status, response, _ = self._call(method, url, body, content_type)
        return status, response

    def ensure_queue(self, name: str) -> bool:
        status, body = self._request("PUT", f"{self.queue_endpoint}/{name}")
        if status not in (201, 204, 409):
            raise AzuriteError(f"create queue {name}: HTTP {status} {body[:200]!r}")
        return status == 201

    def queue_length(self, name: str) -> int | None:
        status, _, headers = self._call("GET", f"{self.queue_endpoint}/{name}?comp=metadata")
        if status == 404:
            return None
        if status != 200:
            raise AzuriteError(f"queue metadata {name}: HTTP {status}")
        lowered = {key.lower(): value for key, value in headers.items()}
        return int(lowered.get("x-ms-approximate-messages-count", "0"))

    def send_message(self, queue: str, text: str) -> None:
        # Functions queue triggers use messageEncoding "none" (host.json), so the text is sent raw.
        escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        body = f"<QueueMessage><MessageText>{escaped}</MessageText></QueueMessage>".encode("utf-8")
        status, response = self._request("POST", f"{self.queue_endpoint}/{queue}/messages", body, "application/xml")
        if status != 201:
            raise AzuriteError(f"send to {queue}: HTTP {status} {response[:200]!r}")

    def clear_messages(self, queue: str) -> None:
        status, body = self._request("DELETE", f"{self.queue_endpoint}/{queue}/messages")
        if status not in (204, 404):
            raise AzuriteError(f"clear {queue}: HTTP {status} {body[:200]!r}")

    def peek_messages(self, queue: str, count: int = 32) -> list[str]:
        status, body = self._request("GET", f"{self.queue_endpoint}/{queue}/messages?numofmessages={min(count, 32)}&peekonly=true")
        if status == 404:
            return []
        if status != 200:
            raise AzuriteError(f"peek {queue}: HTTP {status} {body[:200]!r}")
        root = ElementTree.fromstring(body)
        return [node.findtext("MessageText") or "" for node in root.findall("QueueMessage")]

    def list_queues(self) -> list[str]:
        status, body = self._request("GET", f"{self.queue_endpoint}?comp=list")
        if status != 200:
            raise AzuriteError(f"list queues: HTTP {status}")
        root = ElementTree.fromstring(body)
        return [node.findtext("Name") or "" for node in root.iter("Queue")]


def format_messages(messages: list[str]) -> str:
    lines = []
    for message in messages:
        try:
            lines.append(json.dumps(json.loads(message), ensure_ascii=False))
        except ValueError:
            lines.append(message)
    return "\n".join(lines)
