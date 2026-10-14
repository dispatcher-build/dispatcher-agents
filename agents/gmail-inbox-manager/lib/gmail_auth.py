"""Mint a Gmail refresh token on your own machine.

Run this locally, never inside the worker:

    python lib/gmail_auth.py --client-id <id> --client-secret <secret>

Prerequisites in console.cloud.google.com:
  1. Enable the Gmail API for a project.
  2. OAuth consent screen: External, add your own address as a test user.
     While the screen stays in "Testing", Google expires refresh tokens after
     7 days; publish the app (no verification needed for gmail.modify on your
     own account) to get a long-lived token.
  3. Credentials -> Create OAuth client ID -> Desktop app. Copy the id/secret.

The script opens a browser, catches the redirect on a loopback port, exchanges
the code, and prints the three values to paste into the worker's Config tab.
Standard library only.
"""

import argparse
import http.server
import json
import secrets
import sys
import urllib.parse
import urllib.request
import webbrowser

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/gmail.modify"


class _Catch(http.server.BaseHTTPRequestHandler):
    result = {}

    def do_GET(self):
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        _Catch.result = {k: v[0] for k, v in query.items()}
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<h2>Done. You can close this tab and return to the terminal.</h2>")

    def log_message(self, *args):
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Mint a Gmail refresh token for the Inbox Manager worker")
    parser.add_argument("--client-id", required=True)
    parser.add_argument("--client-secret", required=True)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    redirect_uri = f"http://127.0.0.1:{args.port}/"
    state = secrets.token_urlsafe(16)
    params = {
        "client_id": args.client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    url = f"{AUTH_URL}?{urllib.parse.urlencode(params)}"
    print("Opening your browser. If nothing happens, open this URL:\n\n" + url + "\n")
    webbrowser.open(url)

    server = http.server.HTTPServer(("127.0.0.1", args.port), _Catch)
    server.handle_request()
    server.server_close()
    result = _Catch.result
    if result.get("state") != state or not result.get("code"):
        print("Authorization failed: " + result.get("error", "no code received"), file=sys.stderr)
        return 1

    body = urllib.parse.urlencode({
        "code": result["code"],
        "client_id": args.client_id,
        "client_secret": args.client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }).encode()
    req = urllib.request.Request(TOKEN_URL, data=body, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        token = json.loads(resp.read().decode())
    refresh = token.get("refresh_token")
    if not refresh:
        print("Google returned no refresh token. Revoke the app at myaccount.google.com/permissions and run again.", file=sys.stderr)
        return 1

    print("Paste these into the worker's Config tab:\n")
    print(f"GMAIL_CLIENT_ID={args.client_id}")
    print(f"GMAIL_CLIENT_SECRET={args.client_secret}")
    print(f"GMAIL_REFRESH_TOKEN={refresh}")
    print("\nStart the worker in AUTONOMY=observe first and watch a few triage records.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
