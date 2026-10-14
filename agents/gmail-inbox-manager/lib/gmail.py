"""Gmail REST client: OAuth refresh plus the handful of calls triage needs.

Every request goes to a fixed Google host (oauth2.googleapis.com and
gmail.googleapis.com); nothing here is built from user config, so the
dispatchkit.netsafe guard is not involved. The credential is the user's own
refresh token, exchanged for a short-lived access token on demand.
"""

import time
from email.utils import parseaddr

import requests

TOKEN_URL = "https://oauth2.googleapis.com/token"
API_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
METADATA_HEADERS = ["From", "To", "Subject", "Date", "List-Unsubscribe", "Precedence", "Auto-Submitted", "In-Reply-To"]


class GmailError(RuntimeError):
    """Gmail returned a non-2xx or the network failed."""


class GmailAuthError(GmailError):
    """The refresh token was rejected; the user has to mint a new one."""


class GmailClient:
    def __init__(self, client_id: str, client_secret: str, refresh_token: str, timeout: float = 20.0):
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self.timeout = timeout
        self._access_token = ""
        self._expires_at = 0.0
        self._session = requests.Session()

    def _token(self) -> str:
        if self._access_token and time.time() < self._expires_at - 60:
            return self._access_token
        try:
            resp = self._session.post(TOKEN_URL, data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "refresh_token": self.refresh_token,
                "grant_type": "refresh_token",
            }, timeout=self.timeout)
        except requests.RequestException as e:
            raise GmailError(f"token refresh failed: {e}")
        if resp.status_code != 200:
            body = resp.text[:300]
            if "invalid_grant" in body or resp.status_code in (400, 401):
                raise GmailAuthError(f"refresh token rejected ({resp.status_code}): {body}")
            raise GmailError(f"token refresh HTTP {resp.status_code}: {body}")
        data = resp.json()
        self._access_token = data["access_token"]
        self._expires_at = time.time() + float(data.get("expires_in", 3600))
        return self._access_token

    def _request(self, method: str, path: str, *, params=None, json=None) -> dict:
        for attempt in (1, 2):
            headers = {"Authorization": f"Bearer {self._token()}"}
            try:
                resp = self._session.request(method, f"{API_BASE}/{path}", params=params, json=json,
                                             headers=headers, timeout=self.timeout)
            except requests.RequestException as e:
                raise GmailError(f"{method} {path} failed: {e}")
            if resp.status_code == 401 and attempt == 1:
                self._access_token = ""
                continue
            if resp.status_code == 401:
                raise GmailAuthError(f"{method} {path}: unauthorized after refresh")
            if resp.status_code >= 400:
                raise GmailError(f"{method} {path} HTTP {resp.status_code}: {resp.text[:300]}")
            return resp.json() if resp.content else {}
        return {}

    def profile(self) -> dict:
        return self._request("GET", "profile")

    def list_labels(self) -> list:
        return self._request("GET", "labels").get("labels", [])

    def create_label(self, name: str) -> dict:
        return self._request("POST", "labels", json={
            "name": name,
            "labelListVisibility": "labelShow",
            "messageListVisibility": "show",
        })

    def ensure_labels(self, prefix: str, categories: list) -> dict:
        existing = {label["name"]: label["id"] for label in self.list_labels()}
        if prefix not in existing:
            existing[prefix] = self.create_label(prefix)["id"]
        ids = {}
        for category in categories:
            name = f"{prefix}/{category}"
            if name not in existing:
                existing[name] = self.create_label(name)["id"]
            ids[category] = existing[name]
        return ids

    def list_message_ids(self, query: str, max_results: int) -> list:
        ids = []
        token = None
        while len(ids) < max_results:
            params = {"q": query, "maxResults": min(100, max_results - len(ids))}
            if token:
                params["pageToken"] = token
            data = self._request("GET", "messages", params=params)
            ids.extend(m["id"] for m in data.get("messages", []))
            token = data.get("nextPageToken")
            if not token:
                break
        return ids[:max_results]

    def get_message(self, message_id: str) -> dict:
        data = self._request("GET", f"messages/{message_id}", params=[
            ("format", "metadata"),
            *[("metadataHeaders", h) for h in METADATA_HEADERS],
        ])
        headers = {h["name"].lower(): h["value"] for h in data.get("payload", {}).get("headers", [])}
        sender = headers.get("from", "")
        return {
            "id": data["id"],
            "thread_id": data.get("threadId", ""),
            "from": sender,
            "from_email": parseaddr(sender)[1].lower(),
            "to": headers.get("to", ""),
            "subject": headers.get("subject", "(no subject)"),
            "snippet": data.get("snippet", ""),
            "date": headers.get("date", ""),
            "label_ids": data.get("labelIds", []),
            "headers": headers,
        }

    def modify(self, message_id: str, add: list, remove: list) -> dict:
        body = {}
        if add:
            body["addLabelIds"] = list(add)
        if remove:
            body["removeLabelIds"] = list(remove)
        if not body:
            return {}
        return self._request("POST", f"messages/{message_id}/modify", json=body)
