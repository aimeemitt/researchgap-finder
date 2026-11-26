"""Research Gap Finder as an MCP addon.

Two ways to run it:
- On your own computer (LM Studio, Claude Desktop):  researchgap-finder
  Keys come from mcp.json (NCBI_EMAIL, OPENALEX_API_KEY, optional NCBI_API_KEY).
- Online (Render), for ChatGPT, Claude and others:   researchgap-finder-online
  Every user signs in once on a small page and enters their own email and OpenAlex key.
  Nothing is stored on the server: the keys travel encrypted inside each user's login token.
"""

import base64
import hashlib
import html
import json
import os
import re
import secrets
import time
import zlib

import requests
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    RegistrationError,
    construct_redirect_uri,
)
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.server.mcpserver import MCPServer
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from mcp_types import ToolAnnotations

from core import (
    FIELDS,
    RULES,
    analyzed_markdown,
    get_literature,
    must_read_markdown,
    prompt_intro,
    prompt_sections,
)

NAME = "Research Gap Finder"
ACCESS_TOKEN_SECONDS = 24 * 3600  # 1 day
REFRESH_TOKEN_SECONDS = 180 * 24 * 3600  # 180 days, then the user signs in again
SIGNIN_SECONDS = 15 * 60  # time to fill in the sign-in page
CODE_SECONDS = 5 * 60


def instructions(term, articles, total, field, source, must_read):
    return f"""SEARCH COMPLETE. Do NOT call find_thesis_gaps again. Use the publications below to write your final answer now.

{prompt_intro(term, articles, field)}

Write your answer in Markdown in EXACTLY this layout. Do not add anything before or after it.

# 🔬 Research Gap Finder
*Suggested gaps are starting points – always verify them with your own search and your supervisor.*

> {source} hits in total: {total} · analyzing {len(articles)} publications

{prompt_sections(articles)}

## 5. Must-read papers (most cited)
Copy this list exactly as it is:
{must_read_markdown(must_read)}

---
## Analyzed publications
Copy this list exactly as it is:
{analyzed_markdown(articles)}

{RULES}

REMINDER: The search is already done. Do NOT call any tool again. Write the answer now."""


class UserAccessToken(AccessToken):
    email: str = ""
    openalex_key: str = ""


class UserAuthorizationCode(AuthorizationCode):
    email: str = ""
    openalex_key: str = ""


class UserRefreshToken(RefreshToken):
    email: str = ""
    openalex_key: str = ""


def user_keys():
    """(email, OpenAlex key, NCBI key) of whoever is calling."""
    token = get_access_token()
    if isinstance(token, UserAccessToken):  # online: the signed-in user's own keys
        return token.email, token.openalex_key, ""
    return os.getenv("NCBI_EMAIL", ""), os.getenv("OPENALEX_API_KEY", ""), os.getenv("NCBI_API_KEY", "")


def find_thesis_gaps(keyword: str, field: str = "Medicine", max_results: int = 15) -> str:
    """Search real literature for a keyword and return publications plus instructions
    to suggest doctoral thesis knowledge gaps, plus a must-read list of the most cited papers.

    field must be one of: Medicine, Dentistry, Law, Psychology, Economics / Business,
    Natural sciences, Engineering / Computer science, Social sciences / Humanities.
    Medicine and Dentistry use PubMed; all other fields use OpenAlex.
    """
    if field not in FIELDS:
        return f"Unknown field '{field}'. Use one of: {', '.join(FIELDS)}."
    max_results = max(5, min(int(max_results), 100))
    source = FIELDS[field][2]
    email, openalex_key, ncbi_key = user_keys()
    online = get_access_token() is not None
    if source == "PubMed" and not email:
        return "Setup missing: add NCBI_EMAIL to this addon's settings."
    if source == "OpenAlex" and not openalex_key:
        if online:
            return (
                "This field needs an OpenAlex API key (free at openalex.org). "
                "Disconnect Research Gap Finder in your AI app, connect again and enter the key."
            )
        return "Setup missing: add OPENALEX_API_KEY to this addon's settings."
    try:
        articles, total, must_read, source = get_literature(keyword, field, max_results, email, ncbi_key, openalex_key)
    except requests.RequestException as e:
        status = getattr(getattr(e, "response", None), "status_code", None)
        return f"{source} error" + (f" (HTTP {status})" if status else ": could not connect") + ". Try again later."
    if not articles:
        return f"No {source} results for '{keyword}'. Try a broader keyword."
    return instructions(keyword, articles, total, field, source, must_read)


TOOL_ANNOTATIONS = ToolAnnotations(title=NAME, readOnlyHint=True, destructiveHint=False, openWorldHint=True)


# ---------------- Local (your own computer) ----------------
def main():
    server = MCPServer(NAME)
    server.tool(annotations=TOOL_ANNOTATIONS)(find_thesis_gaps)
    server.run()


# ---------------- Online: sign-in without a database ----------------
class Sealer:
    """Encrypts small pieces of data into strings only this server can read."""

    def __init__(self, secret):
        from cryptography.fernet import Fernet

        key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
        self.fernet = Fernet(key)

    def seal(self, kind, data, seconds=None):
        payload = {"kind": kind, "exp": time.time() + seconds if seconds else None, **data}
        return self.fernet.encrypt(zlib.compress(json.dumps(payload).encode())).decode()

    def open(self, kind, token):
        try:
            payload = json.loads(zlib.decompress(self.fernet.decrypt(token.encode())))
        except Exception:
            return None
        if payload.get("kind") != kind:
            return None
        if payload.get("exp") is not None and payload["exp"] < time.time():
            return None
        return payload


class SignInProvider:
    """OAuth sign-in for ChatGPT / Claude. Each user's keys live encrypted in their own tokens."""

    def __init__(self, base_url, secret):
        self.base_url = base_url.rstrip("/")
        self.sealer = Sealer(secret)
        self.used_codes = {}  # code id -> expiry; stops a code being used twice

    # --- clients (ChatGPT, Claude, ...) register themselves automatically ---
    async def register_client(self, client_info):
        if any(urlhost(str(u)) is None for u in client_info.redirect_uris or []):
            raise RegistrationError("invalid_redirect_uri", "Redirect URIs must be https (or localhost).")
        data = client_info.model_dump(mode="json", exclude={"client_id"})
        client_info.client_id = self.sealer.seal("client", data)

    async def get_client(self, client_id):
        data = self.sealer.open("client", client_id)
        if data is None:
            return None
        data.pop("kind", None)
        data.pop("exp", None)
        return OAuthClientInformationFull.model_validate({**data, "client_id": client_id})

    # --- sign-in page ---
    async def authorize(self, client, params: AuthorizationParams):
        request = self.sealer.seal(
            "signin", {"client_id": client.client_id, **params.model_dump(mode="json")}, SIGNIN_SECONDS
        )
        return f"{self.base_url}/signin?{urlencode_one('request', request)}"

    def finish_signin(self, request, email, openalex_key):
        """Returns the address to send the user back to (their AI app)."""
        data = self.sealer.open("signin", request)
        if data is None:
            return None
        code = self.sealer.seal(
            "code",
            {
                "id": secrets.token_urlsafe(16),
                "client_id": data["client_id"],
                "scopes": data.get("scopes") or [],
                "code_challenge": data["code_challenge"],
                "redirect_uri": data["redirect_uri"],
                "redirect_uri_provided_explicitly": data["redirect_uri_provided_explicitly"],
                "resource": data.get("resource"),
                "email": email,
                "openalex_key": openalex_key,
            },
            CODE_SECONDS,
        )
        return construct_redirect_uri(data["redirect_uri"], code=code, state=data.get("state"))

    async def load_authorization_code(self, client, authorization_code):
        data = self.sealer.open("code", authorization_code)
        if data is None or data["client_id"] != client.client_id or data["id"] in self.used_codes:
            return None
        return UserAuthorizationCode(
            code=authorization_code,
            scopes=data["scopes"],
            expires_at=data["exp"],
            client_id=data["client_id"],
            code_challenge=data["code_challenge"],
            redirect_uri=data["redirect_uri"],
            redirect_uri_provided_explicitly=data["redirect_uri_provided_explicitly"],
            resource=data.get("resource"),
            email=data["email"],
            openalex_key=data["openalex_key"],
        )

    async def exchange_authorization_code(self, client, authorization_code):
        data = self.sealer.open("code", authorization_code.code)
        if data is None:
            from mcp.server.auth.provider import TokenError

            raise TokenError("invalid_grant", "Sign-in code expired. Please connect again.")
        now = time.time()
        self.used_codes = {k: v for k, v in self.used_codes.items() if v > now}
        self.used_codes[data["id"]] = data["exp"]
        return self._tokens(client.client_id, authorization_code.scopes, authorization_code.resource,
                            authorization_code.email, authorization_code.openalex_key)

    async def load_refresh_token(self, client, refresh_token):
        data = self.sealer.open("refresh", refresh_token)
        if data is None or data["client_id"] != client.client_id:
            return None
        return UserRefreshToken(
            token=refresh_token,
            client_id=data["client_id"],
            scopes=data["scopes"],
            expires_at=int(data["exp"]),
            resource=data.get("resource"),
            email=data["email"],
            openalex_key=data["openalex_key"],
        )

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        return self._tokens(client.client_id, scopes or refresh_token.scopes, refresh_token.resource,
                            refresh_token.email, refresh_token.openalex_key)

    async def load_access_token(self, token):
        data = self.sealer.open("access", token)
        if data is None:
            return None
        return UserAccessToken(
            token=token,
            client_id=data["client_id"],
            scopes=data["scopes"],
            expires_at=int(data["exp"]),
            resource=data.get("resource"),
            email=data["email"],
            openalex_key=data["openalex_key"],
        )

    async def revoke_token(self, token):
        pass  # tokens are not stored, so there is nothing to delete; they expire on their own

    def _tokens(self, client_id, scopes, resource, email, openalex_key):
        data = {"client_id": client_id, "scopes": scopes, "resource": resource,
                "email": email, "openalex_key": openalex_key}
        return OAuthToken(
            access_token=self.sealer.seal("access", data, ACCESS_TOKEN_SECONDS),
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_SECONDS,
            refresh_token=self.sealer.seal("refresh", data, REFRESH_TOKEN_SECONDS),
            scope=" ".join(scopes) if scopes else None,
        )


def urlencode_one(key, value):
    from urllib.parse import urlencode

    return urlencode({key: value})


def urlhost(url):
    from urllib.parse import urlparse

    p = urlparse(url)
    if p.scheme == "https" or (p.scheme == "http" and p.hostname in ("localhost", "127.0.0.1")):
        return p.hostname
    return None


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def signin_page(request, client_name, app_host, error=""):
    e = html.escape
    error_html = f'<p class="error">{e(error)}</p>' if error else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Connect {e(NAME)}</title>
<style>
body{{font-family:-apple-system,system-ui,sans-serif;max-width:460px;margin:40px auto;padding:0 20px;color:#222}}
h1{{font-size:1.4em}} label{{display:block;margin-top:18px;font-weight:600}}
input{{width:100%;padding:10px;margin-top:6px;font-size:1em;box-sizing:border-box;border:1px solid #bbb;border-radius:6px}}
small{{color:#555;display:block;margin-top:4px}} button{{margin-top:24px;width:100%;padding:12px;font-size:1em;
background:#1a7f37;color:#fff;border:0;border-radius:6px;cursor:pointer}} .error{{color:#b00020}}
.box{{background:#f4f4f4;padding:10px 14px;border-radius:6px}}
</style></head><body>
<h1>🔬 Connect {e(NAME)}</h1>
<p class="box">{e(client_name or "Your AI app")} ({e(app_host or "unknown")}) wants to use {e(NAME)}.</p>
{error_html}
<form method="post" action="/signin">
<input type="hidden" name="request" value="{e(request)}">
<label for="email">Your email</label>
<input id="email" name="email" type="email" required autocomplete="email">
<small>Required by PubMed (the US National Library of Medicine) for searches.</small>
<label for="openalex_key">OpenAlex API key <span style="font-weight:400">(optional)</span></label>
<input id="openalex_key" name="openalex_key" type="text" autocomplete="off">
<small>Only needed for fields other than Medicine and Dentistry.
Free at <a href="https://openalex.org" target="_blank" rel="noopener">openalex.org</a> → sign up → Settings → API key.</small>
<button type="submit">Connect</button>
</form>
<p><small>Your email and key are not stored on this server. They are encrypted inside your AI app's connection.
To change them, disconnect {e(NAME)} in your AI app and connect again.
<a href="/privacy" target="_blank">Privacy policy</a> · <a href="/terms" target="_blank">Terms of use</a></small></p>
</body></html>"""


REPO_URL = "https://github.com/aimeemitt/researchgap-finder"
PAGE_STYLE = """body{font-family:-apple-system,system-ui,sans-serif;max-width:640px;margin:40px auto;padding:0 20px;color:#222;line-height:1.5}
code{background:#f4f4f4;padding:4px 8px;border-radius:4px;word-break:break-all} h2{font-size:1.15em;margin-top:28px}
footer{margin-top:40px;font-size:.9em;color:#555}"""


def contact():
    """Developer name and support email, set on Render (DEVELOPER_NAME, SUPPORT_EMAIL)."""
    return os.getenv("DEVELOPER_NAME", "The developer of Research Gap Finder"), os.getenv("SUPPORT_EMAIL", "")


def page(title, body):
    e = html.escape
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(title)}</title><style>{PAGE_STYLE}</style></head><body>
{body}
<footer><a href="/">Home</a> · <a href="/privacy">Privacy policy</a> · <a href="/terms">Terms of use</a> · <a href="/support">Support</a></footer>
</body></html>"""


def home_page(mcp_url):
    e = html.escape
    return page(f"{NAME} addon", f"""<h1>🔬 {e(NAME)} – addon for your AI</h1>
<p>Helps students find a doctoral thesis topic. It searches real literature (PubMed for medicine and dentistry,
OpenAlex for other fields), and your AI suggests possible knowledge gaps backed by real references,
plus a reading list of the most cited papers.</p>
<p>Add this address to your AI app (ChatGPT, Claude, …):</p>
<p><code>{e(mcp_url)}</code></p>
<p>Step-by-step instructions: <a href="{REPO_URL}#readme">{e(REPO_URL.removeprefix("https://"))}</a></p>""")


def privacy_page():
    e = html.escape
    name, email = contact()
    reach = f'<a href="mailto:{e(email)}">{e(email)}</a>' if email else f'<a href="{REPO_URL}/issues">GitHub</a>'
    return page(f"Privacy policy – {NAME}", f"""<h1>Privacy policy</h1>
<p>Last updated: 26 November 2026</p>
<h2>Who is responsible</h2>
<p>{e(name)}. Contact: {reach}.</p>
<h2>What data is processed, and why</h2>
<ul>
<li><b>Your email address</b> (entered once on the sign-in page). Sent to PubMed (US National Library of Medicine),
which requires an email address for every search.</li>
<li><b>Your OpenAlex API key</b> (optional, entered once on the sign-in page). Sent to OpenAlex to search
non-medical literature.</li>
<li><b>Your search terms and field</b> (sent by your AI app when you ask for a search). Sent to PubMed, NIH iCite
(citation counts) or OpenAlex to find publications.</li>
<li><b>Technical data</b> (IP address, time, requested page) in the hosting provider's server logs, needed to run
and protect the service.</li>
</ul>
<p>Nothing else is collected. No account, no payment data, no chat history, no tracking, no advertising.</p>
<h2>Where it is stored</h2>
<p>Your email and key are <b>not stored on this server</b>. They are encrypted and kept only inside the
connection your AI app holds (the login token). Search terms and results are not stored by this service.</p>
<h2>Who receives data</h2>
<ul>
<li>PubMed / NCBI and NIH iCite (U.S. National Institutes of Health): search terms and your email.</li>
<li>OpenAlex (OurResearch): search terms and your OpenAlex key.</li>
<li>Render (hosting, USA): technical server logs.</li>
<li>Your AI app (e.g. ChatGPT, Claude) receives the search results; its own privacy policy applies.</li>
</ul>
<p>Some of these recipients are in the USA, so data may be processed outside the EU.</p>
<h2>How long</h2>
<p>Your login expires after 180 days at the latest; you can end it any time by removing the addon in your AI app.
Server logs are kept by the hosting provider for a limited time according to its own policy.</p>
<h2>Your rights</h2>
<p>You can ask for information, correction or deletion of your data and object to its processing, and you can
complain to a data protection authority. Because this service stores no personal data itself, removing the
addon in your AI app ends all processing by this service.</p>""")


def terms_page():
    e = html.escape
    return page(f"Terms of use – {NAME}", f"""<h1>Terms of use</h1>
<p>Last updated: 26 November 2026</p>
<ul>
<li>{e(NAME)} is free to use. It helps you find literature and possible research gaps.</li>
<li>Suggested gaps are starting points, not facts. Always check them with your own literature search and your
supervisor. The service gives no medical, legal or professional advice.</li>
<li>Results depend on PubMed, NIH iCite and OpenAlex and on your AI app. They can be incomplete or wrong.</li>
<li>The service is provided "as is", without any warranty, and may change or stop at any time.</li>
<li>Use it lawfully and do not overload or misuse it. You must follow the terms of PubMed, OpenAlex and your AI app.</li>
<li>The source code is open source under the MIT license: <a href="{REPO_URL}">{e(REPO_URL.removeprefix("https://"))}</a>.</li>
</ul>""")


def support_page():
    e = html.escape
    name, email = contact()
    mail = f'<li>Email: <a href="mailto:{e(email)}">{e(email)}</a></li>' if email else ""
    return page(f"Support – {NAME}", f"""<h1>Support</h1>
<p>Questions, problems or ideas? Get in touch:</p>
<ul>{mail}<li>GitHub: <a href="{REPO_URL}/issues">report a problem</a></li></ul>
<h2>Common problems</h2>
<ul>
<li><b>The first search fails or is slow:</b> the service sleeps when unused and needs about a minute to wake up. Ask again.</li>
<li><b>"This field needs an OpenAlex API key":</b> get a free key at openalex.org, remove the addon in your AI app,
add it again and enter the key.</li>
<li><b>Change your email or key:</b> remove the addon in your AI app and add it again.</li>
</ul>""")


def main_online():
    import uvicorn
    from starlette.requests import Request
    from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse

    secret = os.getenv("SECRET_KEY", "")
    if len(secret) < 32:
        raise SystemExit("Set SECRET_KEY (at least 32 characters) before starting the online addon.")
    port = int(os.getenv("PORT", "10000"))
    base_url = (os.getenv("PUBLIC_URL") or os.getenv("RENDER_EXTERNAL_URL") or f"http://localhost:{port}").rstrip("/")
    mcp_url = base_url + "/mcp"

    provider = SignInProvider(base_url, secret)
    server = MCPServer(
        NAME,
        auth_server_provider=provider,
        auth=AuthSettings(
            issuer_url=base_url,
            resource_server_url=mcp_url,
            validate_token_resource=False,
            client_registration_options=ClientRegistrationOptions(enabled=True),
        ),
    )
    server.tool(annotations=TOOL_ANNOTATIONS)(find_thesis_gaps)

    @server.custom_route("/", methods=["GET"])
    async def home(request: Request):
        return HTMLResponse(home_page(mcp_url))

    @server.custom_route("/privacy", methods=["GET"])
    async def privacy(request: Request):
        return HTMLResponse(privacy_page())

    @server.custom_route("/terms", methods=["GET"])
    async def terms(request: Request):
        return HTMLResponse(terms_page())

    @server.custom_route("/support", methods=["GET"])
    async def support(request: Request):
        return HTMLResponse(support_page())

    @server.custom_route("/.well-known/openai-apps-challenge", methods=["GET"])
    async def openai_challenge(request: Request):
        # Domain check for the ChatGPT app directory: paste the token from OpenAI into OPENAI_APPS_CHALLENGE on Render.
        token = os.getenv("OPENAI_APPS_CHALLENGE", "").strip()
        return PlainTextResponse(token, status_code=200 if token else 404)

    @server.custom_route("/signin", methods=["GET", "POST"])
    async def signin(request: Request):
        if request.method == "GET":
            sealed = request.query_params.get("request", "")
            email = openalex_key = ""
        else:
            form = await request.form()
            sealed = str(form.get("request", ""))
            email = str(form.get("email", "")).strip()
            openalex_key = str(form.get("openalex_key", "")).strip()

        data = provider.sealer.open("signin", sealed)
        if data is None:
            return HTMLResponse("<h1>This sign-in link has expired.</h1><p>Go back to your AI app and connect again.</p>",
                                status_code=400)
        client = await provider.get_client(data["client_id"])
        client_name = client.client_name if client else ""
        app_host = urlhost(data["redirect_uri"])

        if request.method == "GET":
            return HTMLResponse(signin_page(sealed, client_name, app_host))
        if not EMAIL_RE.match(email) or len(email) > 200 or len(openalex_key) > 200:
            return HTMLResponse(signin_page(sealed, client_name, app_host, "Please enter a valid email."),
                                status_code=400)
        return RedirectResponse(provider.finish_signin(sealed, email, openalex_key), status_code=302)

    app = server.streamable_http_app(host="0.0.0.0", stateless_http=True, json_response=True)
    uvicorn.run(app, host="0.0.0.0", port=port, proxy_headers=True, forwarded_allow_ips="*")


if __name__ == "__main__":
    main()
