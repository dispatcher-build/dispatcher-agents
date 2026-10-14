"""
Send backends. Two, behind one interface.

  resend   an API key and a verified domain. Nothing to install, no review
           queue, works minutes after signup. The default.
  ses      cheaper at volume and what you already have if you are on AWS, but
           it needs an account, a verified domain with DKIM, and a support
           review to leave the sandbox.

Every provider answers three questions: can I send (preflight), send this one
message, and is this failure the recipient's fault. That last one decides
whether an address is burned or retried, so each provider has to answer it in
its own vocabulary.
"""

import json
import re
import urllib.error
import urllib.request
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

TAG_RE = re.compile(r"<[^>]+>")

RESEND_API = "https://api.resend.com"


def html_to_text(html: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?</\1>", "", html)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p>", "\n\n", text)
    text = TAG_RE.sub("", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def unsubscribe_headers(mailto: str, url: str) -> dict:
    targets = [f"<{t}>" for t in (f"mailto:{mailto}" if mailto else "", url) if t]
    if not targets:
        return {}
    return {
        "List-Unsubscribe": ", ".join(targets),
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
    }


class ProviderError(RuntimeError):
    def __init__(self, message, recipient_fault=False):
        super().__init__(message)
        self.recipient_fault = recipient_fault


# --- Resend ------------------------------------------------------------------

# Resend rejects the message when the address itself is the problem; anything
# else (401, 403, 429, 5xx) is ours to fix and must not burn the recipient.
RESEND_RECIPIENT_FAULT = re.compile(
    r"invalid[_ ]?(to|recipient|email)|not a valid email|suppress", re.I
)


class ResendProvider:
    name = "resend"

    def __init__(self, config: dict):
        self.api_key = (config.get("api_key") or "").strip()
        self.timeout = float(config.get("timeout", 30))

    def _request(self, method: str, path: str, body: dict = None):
        req = urllib.request.Request(
            f"{RESEND_API}{path}",
            data=json.dumps(body).encode() if body is not None else None,
            method=method,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "User-Agent": "dispatcher-outbound-marketing-email",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as res:
                return json.loads(res.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            raw = e.read().decode(errors="replace")
            try:
                detail = json.loads(raw).get("message") or raw
            except json.JSONDecodeError:
                detail = raw
            raise ProviderError(
                f"{e.code}: {detail}",
                recipient_fault=bool(RESEND_RECIPIENT_FAULT.search(detail)),
            ) from None
        except urllib.error.URLError as e:
            raise ProviderError(f"cannot reach Resend ({e.reason})") from None

    def preflight(self, from_email: str) -> dict:
        result = {"provider": self.name, "ok": False, "problems": [], "notes": [],
                  "credentials": "api key" if self.api_key else "", "from_email": from_email}
        if not self.api_key:
            result["problems"].append(
                "No Resend API key. Create one at resend.com/api-keys and paste it into "
                "RESEND_API_KEY in this worker's config."
            )
            return result

        try:
            domains = self._request("GET", "/domains").get("data") or []
        except ProviderError as e:
            message = str(e)
            # Resend answers a bad key with 400 "API key is invalid", not 401.
            if re.search(r"^(401|403)|api[_ ]?key", message, re.I):
                result["problems"].append(
                    "Resend rejected the API key. Create one at resend.com/api-keys and paste it "
                    "into RESEND_API_KEY."
                )
            else:
                result["problems"].append(f"Could not read your Resend domains: {message}")
            return result

        result["domains"] = [
            {"name": d.get("name"), "status": d.get("status")} for d in domains
        ]
        if from_email:
            domain = from_email.split("@")[-1].lower()
            match = next((d for d in domains if (d.get("name") or "").lower() == domain), None)
            if match is None:
                result["problems"].append(
                    f"{domain} is not a domain on this Resend account. Add it at "
                    f"resend.com/domains and add the DNS records it gives you."
                )
            elif (match.get("status") or "").lower() != "verified":
                result["problems"].append(
                    f"The domain {domain} is '{match.get('status')}' on Resend, not verified. "
                    f"Finish its DNS records before sending."
                )
            else:
                result["identity"] = domain
        result["ok"] = not result["problems"]
        return result

    def send(self, message: dict) -> str:
        body = {
            "from": formataddr((message["from_name"], message["from_email"]))
                    if message.get("from_name") else message["from_email"],
            "to": [message["to"]],
            "subject": message["subject"],
        }
        if message.get("is_html"):
            body["html"] = message["body"]
            body["text"] = html_to_text(message["body"])
        else:
            body["text"] = message["body"]
        if message.get("reply_to"):
            body["reply_to"] = message["reply_to"]
        headers = unsubscribe_headers(message.get("unsubscribe_mailto", ""),
                                      message.get("unsubscribe_url", ""))
        if headers:
            body["headers"] = headers
        return (self._request("POST", "/emails", body) or {}).get("id", "")


# --- Amazon SES --------------------------------------------------------------

SES_RECIPIENT_FAULT = re.compile(
    r"blacklist|suppress(ed|ion) list|invalid domain|domain contains|"
    r"local address contains|not a valid|illegal address|recipient address",
    re.I,
)


class SesProvider:
    name = "ses"

    def __init__(self, config: dict):
        self.region = (config.get("region") or "us-east-1").strip()
        self._client = None
        self._client_v2 = None

    def _boto(self, service):
        import boto3

        return boto3.client(service, region_name=self.region)

    @property
    def client(self):
        if self._client is None:
            self._client = self._boto("ses")
        return self._client

    @property
    def client_v2(self):
        if self._client_v2 is None:
            self._client_v2 = self._boto("sesv2")
        return self._client_v2

    def _credential_source(self) -> str:
        try:
            import boto3

            creds = boto3.Session(region_name=self.region).get_credentials()
        except Exception:
            return ""
        return getattr(creds, "method", "unknown") if creds else ""

    def preflight(self, from_email: str) -> dict:
        result = {"provider": self.name, "ok": False, "problems": [], "notes": [],
                  "credentials": self._credential_source(), "region": self.region,
                  "from_email": from_email}
        if not result["credentials"]:
            result["problems"].append(
                "No AWS credentials. Paste an access key for an IAM user with "
                "ses:SendRawEmail into AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY."
            )
            return result

        try:
            account = self.client_v2.get_account()
        except Exception as e:
            result["problems"].append(f"Could not read the SES account: {e}")
            return result

        quota = account.get("SendQuota") or {}
        result["quota"] = {
            "max_24_hour": quota.get("Max24HourSend"),
            "sent_24_hour": quota.get("SentLast24Hours"),
            "max_per_second": quota.get("MaxSendRate"),
        }
        result["production_access"] = bool(account.get("ProductionAccessEnabled"))
        if not account.get("SendingEnabled"):
            result["problems"].append("SES sending is disabled for this account.")
        if not result["production_access"]:
            result["problems"].append(
                "SES is in sandbox mode, so only verified addresses can receive mail. "
                "Request production access in the SES console."
            )
        enforcement = account.get("EnforcementStatus", "")
        if enforcement and enforcement != "HEALTHY":
            result["notes"].append(f"Account reputation status is {enforcement}.")

        if from_email:
            domain = from_email.split("@")[-1]
            verified = None
            for identity in (from_email, domain):
                try:
                    data = self.client_v2.get_email_identity(EmailIdentity=identity)
                except Exception:
                    continue
                verified = bool(data.get("VerifiedForSendingStatus"))
                result["identity"] = identity
                result["dkim"] = (data.get("DkimAttributes") or {}).get("Status", "")
                break
            if verified is None:
                result["problems"].append(
                    f"Neither {from_email} nor {domain} is an SES identity in {self.region}."
                )
            elif not verified:
                result["problems"].append(f"SES identity {result.get('identity')} is not verified.")
            elif result.get("dkim") not in ("SUCCESS", ""):
                result["notes"].append(f"DKIM status is {result['dkim']} — deliverability will suffer.")

        result["ok"] = not result["problems"]
        return result

    def suppressed(self, limit: int = 20000) -> list:
        """Addresses SES suppressed account-wide after a bounce or complaint."""
        out, token = [], None
        while len(out) < limit:
            kwargs = {"PageSize": 1000}
            if token:
                kwargs["NextToken"] = token
            page = self.client_v2.list_suppressed_destinations(**kwargs)
            out.extend(d["EmailAddress"].lower()
                       for d in page.get("SuppressedDestinationSummaries", [])
                       if d.get("EmailAddress"))
            token = page.get("NextToken")
            if not token:
                break
        return out

    def send(self, message: dict) -> str:
        msg = EmailMessage()
        msg["Subject"] = message["subject"]
        msg["From"] = (formataddr((message["from_name"], message["from_email"]))
                       if message.get("from_name") else message["from_email"])
        msg["To"] = message["to"]
        if message.get("reply_to"):
            msg["Reply-To"] = message["reply_to"]
        msg["Message-ID"] = make_msgid(domain=message["from_email"].split("@")[-1])
        for key, value in unsubscribe_headers(message.get("unsubscribe_mailto", ""),
                                              message.get("unsubscribe_url", "")).items():
            msg[key] = value

        if message.get("is_html"):
            msg.set_content(html_to_text(message["body"]))
            msg.add_alternative(message["body"], subtype="html")
        else:
            msg.set_content(message["body"])

        try:
            response = self.client.send_raw_email(
                Source=message["from_email"],
                Destinations=[message["to"]],
                RawMessage={"Data": msg.as_bytes()},
            )
        except Exception as e:
            raise ProviderError(str(e), recipient_fault=bool(SES_RECIPIENT_FAULT.search(str(e)))) from None
        return response.get("MessageId", "")


PROVIDERS = {"resend": ResendProvider, "ses": SesProvider}


def build(name: str, config: dict):
    factory = PROVIDERS.get((name or "resend").strip().lower())
    if factory is None:
        raise ProviderError(f"unknown provider '{name}' (expected one of: {', '.join(PROVIDERS)})")
    return factory(config)
