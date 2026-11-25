import base64
import hashlib
import json
import os
import secrets
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import jwt
import requests
import streamlit as st
from google import genai

from core import FIELDS, build_prompt, get_literature, must_read_markdown

DEFAULT_MODEL = "gemini-3.8-flash"
DEFAULT_CLAUDE_MODEL = "claude-sonnet-5-5"
DEFAULT_OLLAMA_URL = "http://localhost:11434/v1"
DEFAULT_OLLAMA_MODEL = "llama3.1"

PROVIDERS = [
    "ChatGPT (your Plus/Pro plan)",
    "Claude (your API key)",
    "Gemini (your free API key)",
    "Local (Ollama)",
]


# ---------------- AI: Gemini ----------------
def ask_gemini(prompt, api_key, model):
    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(model=model, contents=prompt)
    return response.text


# ---------------- AI: Claude ----------------
def ask_claude(prompt, api_key, model):
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": model,
            "max_tokens": 8000,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=300,
    )
    if r.status_code != 200:
        raise RuntimeError(f"Claude error {r.status_code}: {r.text}")
    return "".join(block.get("text", "") for block in r.json().get("content", []) if block.get("type") == "text")


# ---------------- AI: Local (Ollama) ----------------
def ask_ollama(prompt, base_url, model):
    r = requests.post(
        base_url.rstrip("/") + "/chat/completions",
        json={"model": model, "messages": [{"role": "user", "content": prompt}]},
        timeout=900,
    )
    if r.status_code != 200:
        raise RuntimeError(f"Ollama error {r.status_code}: {r.text}")
    return r.json()["choices"][0]["message"]["content"]


# ---------------- AI: ChatGPT (Sign in with ChatGPT) ----------------
OPENAI_AUTH = "https://auth.openai.com"
OPENAI_AUTHORIZE = OPENAI_AUTH + "/api/accounts/authorize"
OPENAI_TOKEN = OPENAI_AUTH + "/api/accounts/oauth/token"
OPENAI_OIDC_CONFIG = OPENAI_AUTH + "/.well-known/openid-configuration"
OPENAI_API = "https://api.openai.com/v1"
CHATGPT_SCOPE = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
CHATGPT_PLAN_SCOPE = "chatgpt.tokens.use.direct"
CALLBACK_PATH = "/auth/callback"
CALLBACK_PORT = 1455
APP_DIR = Path.home() / ".research-gap-finder"
CHATGPT_FILE = APP_DIR / "chatgpt.json"
_refresh_lock = threading.Lock()


def _b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _load_chatgpt():
    if CHATGPT_FILE.exists():
        return json.loads(CHATGPT_FILE.read_text())
    return {}


def _save_chatgpt(data):
    APP_DIR.mkdir(mode=0o700, exist_ok=True)
    tmp = CHATGPT_FILE.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.replace(tmp, CHATGPT_FILE)


def _oidc_config():
    r = requests.get(OPENAI_OIDC_CONFIG, timeout=30)
    r.raise_for_status()
    return r.json()


def _verify_id_token(id_token, client_id, nonce):
    config = _oidc_config()
    signing_key = jwt.PyJWKClient(config["jwks_uri"]).get_signing_key_from_jwt(id_token)
    claims = jwt.decode(
        id_token,
        signing_key.key,
        algorithms=["RS256"],
        audience=client_id,
        issuer=config["issuer"],
    )
    if claims.get("nonce") != nonce:
        raise RuntimeError("ChatGPT sign-in failed: nonce mismatch.")
    return claims


def _wait_for_callback(server, timeout=300):
    result = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path != CALLBACK_PATH:
                self.send_response(404)
                self.end_headers()
                return
            result.update({k: v[0] for k, v in parse_qs(parsed.query).items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("<h2>Done. You can close this tab and go back to the app.</h2>".encode())

        def log_message(self, *args):
            pass

    server.RequestHandlerClass = Handler
    server.timeout = 1
    deadline = time.time() + timeout
    while not result and time.time() < deadline:
        server.handle_request()
    server.server_close()
    return result


def chatgpt_sign_in():
    saved = _load_chatgpt()
    host_id = saved.get("host_id") or f"urn:uuid:{uuid.uuid4()}"
    saved_client_id = saved.get("client_id")

    try:
        server = HTTPServer(("127.0.0.1", CALLBACK_PORT), BaseHTTPRequestHandler)
    except OSError:
        server = HTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    redirect_uri = f"http://127.0.0.1:{server.server_address[1]}{CALLBACK_PATH}"

    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)

    params = {
        "response_type": "code",
        "client_id": saved_client_id or "dynamic_agent_client",
        "redirect_uri": redirect_uri,
        "scope": CHATGPT_SCOPE,
        "resource": OPENAI_API,
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "ext_agent_host_id": host_id,
    }
    if saved_client_id:
        if saved.get("id_token"):
            params["id_token_hint"] = saved["id_token"]
        if saved.get("email"):
            params["login_hint"] = saved["email"]
    else:
        params["agent_name_hint"] = "Research Gap Finder"

    webbrowser.open(OPENAI_AUTHORIZE + "?" + urlencode(params))
    cb = _wait_for_callback(server)

    if not cb:
        raise RuntimeError("ChatGPT sign-in timed out. Try again.")
    if cb.get("state") != state:
        raise RuntimeError("ChatGPT sign-in failed: state mismatch.")
    if cb.get("error"):
        raise RuntimeError(f"ChatGPT sign-in cancelled: {cb['error']}")

    if saved_client_id:
        if cb.get("client_id") and cb["client_id"] != saved_client_id:
            raise RuntimeError("ChatGPT sign-in failed: unexpected client ID.")
        client_id = saved_client_id
    else:
        client_id = cb.get("client_id")
        if not client_id:
            raise RuntimeError("ChatGPT registration incomplete. Try again.")

    r = requests.post(
        OPENAI_TOKEN,
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": cb["code"],
            "code_verifier": verifier,
            "redirect_uri": redirect_uri,
            "resource": OPENAI_API,
        },
        timeout=30,
    )
    if r.status_code != 200:
        raise RuntimeError(f"ChatGPT sign-in failed: {r.text}")
    tokens = r.json()

    claims = _verify_id_token(tokens["id_token"], client_id, nonce)
    granted = (tokens.get("scope") or cb.get("scope") or "").split()
    if CHATGPT_PLAN_SCOPE not in granted:
        raise RuntimeError("You did not allow the app to use your ChatGPT plan. Sign in again and allow it.")

    _save_chatgpt(
        {
            "host_id": host_id,
            "client_id": client_id,
            "sub": claims.get("sub"),
            "email": claims.get("email", ""),
            "id_token": tokens["id_token"],
            "access_token": tokens["access_token"],
            "refresh_token": tokens.get("refresh_token"),
            "expires_at": time.time() + int(tokens.get("expires_in", 3600)),
            "scope": " ".join(granted),
        }
    )


def chatgpt_access_token():
    with _refresh_lock:
        data = _load_chatgpt()
        if not data.get("access_token"):
            return None
        if time.time() < data["expires_at"] - 60:
            return data["access_token"]
        r = requests.post(
            OPENAI_TOKEN,
            data={
                "grant_type": "refresh_token",
                "client_id": data["client_id"],
                "refresh_token": data["refresh_token"],
                "resource": OPENAI_API,
            },
            timeout=30,
        )
        if r.status_code != 200:
            raise RuntimeError("Your ChatGPT session expired. Sign in again.")
        tokens = r.json()
        data["access_token"] = tokens["access_token"]
        data["expires_at"] = time.time() + int(tokens.get("expires_in", 3600))
        if tokens.get("refresh_token"):
            data["refresh_token"] = tokens["refresh_token"]
        if tokens.get("scope"):
            data["scope"] = tokens["scope"]
        _save_chatgpt(data)
        return data["access_token"]


def chatgpt_sign_out():
    data = _load_chatgpt()
    if data.get("refresh_token"):
        try:
            requests.post(
                _oidc_config()["revocation_endpoint"],
                data={
                    "token": data["refresh_token"],
                    "token_type_hint": "refresh_token",
                    "client_id": data["client_id"],
                },
                timeout=30,
            )
        except Exception:
            pass
    for key in ("access_token", "refresh_token", "expires_at", "scope"):
        data.pop(key, None)
    _save_chatgpt(data)


@st.cache_data(ttl=3600, show_spinner=False)
def chatgpt_models(access_token):
    r = requests.get(OPENAI_API + "/models", headers={"Authorization": f"Bearer {access_token}"}, timeout=30)
    r.raise_for_status()
    body = r.json()
    items = (body.get("data") or body.get("models") or []) if isinstance(body, dict) else body
    return [m.get("slug") or m.get("id") for m in items if m.get("visibility") == "list"]


def ask_chatgpt(prompt, model):
    token = chatgpt_access_token()
    if not token:
        raise RuntimeError("Sign in with ChatGPT first.")
    r = requests.post(
        OPENAI_API + "/responses",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={
            "model": model,
            "input": [{"role": "user", "content": prompt}],
            "store": False,
            "stream": True,
        },
        stream=True,
        timeout=600,
    )
    if r.status_code == 429 and "subscription_sharing_usage_limit_exceeded" in r.text:
        raise RuntimeError("Your ChatGPT plan limit for this app is reached. Try again later.")
    if r.status_code != 200:
        raise RuntimeError(f"ChatGPT error {r.status_code}: {r.text}")

    text = []
    for line in r.iter_lines(decode_unicode=True):
        if not line or not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            break
        event = json.loads(payload)
        etype = event.get("type")
        if etype == "response.output_text.delta":
            text.append(event.get("delta", ""))
        elif etype == "response.completed":
            return "".join(text)
        elif etype == "response.failed":
            err = (event.get("response") or {}).get("error") or {}
            if err.get("code") == "subscription_sharing_usage_limit_exceeded":
                raise RuntimeError("Your ChatGPT plan limit for this app is reached. Try again later.")
            raise RuntimeError(f"ChatGPT error: {err.get('message', 'request failed')}")
        elif etype == "error":
            raise RuntimeError(f"ChatGPT error: {event.get('message', 'request failed')}")
    raise RuntimeError("ChatGPT response was incomplete. Try again.")


# ---------------- UI ----------------
st.set_page_config(page_title="Research Gap Finder", page_icon="🔬", layout="wide")
st.title("🔬 Research Gap Finder")
st.caption("Prototype. Suggested gaps are starting points – always verify them with your own search and your supervisor.")

with st.sidebar:
    st.header("AI provider")
    provider = st.radio("Choose your AI", PROVIDERS, label_visibility="collapsed")

    gemini_key = claude_key = ""
    model = ""
    ollama_url = DEFAULT_OLLAMA_URL

    if provider == PROVIDERS[0]:
        st.caption("Works when the app runs on your own computer.")
        try:
            chatgpt_token = chatgpt_access_token()
        except Exception as e:
            st.warning(str(e))
            chatgpt_token = None
        if chatgpt_token:
            st.success(f"Signed in as {_load_chatgpt().get('email', 'ChatGPT user')}")
            try:
                models = chatgpt_models(chatgpt_token)
            except Exception as e:
                models = []
                st.warning(f"Could not load models: {e}")
            if models:
                model = st.selectbox("ChatGPT model", models)
            else:
                model = st.text_input("ChatGPT model")
            if st.button("Sign out"):
                chatgpt_sign_out()
                st.rerun()
        else:
            if st.button("Continue with ChatGPT", type="primary"):
                with st.spinner("Finish signing in in the browser tab that just opened..."):
                    try:
                        chatgpt_sign_in()
                        st.rerun()
                    except Exception as e:
                        st.error(str(e))

    elif provider == PROVIDERS[1]:
        claude_key = st.text_input("Claude API key", value=os.getenv("ANTHROPIC_API_KEY", ""), type="password")
        st.caption("Create one at console.anthropic.com (paid per use).")
        model = st.text_input("Claude model", value=DEFAULT_CLAUDE_MODEL)

    elif provider == PROVIDERS[2]:
        gemini_key = st.text_input("Gemini API key", value=os.getenv("GEMINI_API_KEY", ""), type="password")
        st.caption("Create one for free at aistudio.google.com.")
        model = st.text_input("Gemini model", value=os.getenv("GEMINI_MODEL", DEFAULT_MODEL))

    else:
        ollama_url = st.text_input("Ollama URL", value=DEFAULT_OLLAMA_URL)
        model = st.text_input("Ollama model", value=DEFAULT_OLLAMA_MODEL)

    st.header("Settings")
    email = st.text_input("Your email (required by NCBI for medicine)", value=os.getenv("NCBI_EMAIL", ""))
    openalex_key = st.text_input(
        "OpenAlex API key (required for non-medical fields)",
        value=os.getenv("OPENALEX_API_KEY", ""),
        type="password",
    )
    ncbi_key = st.text_input("NCBI API key (optional)", value=os.getenv("NCBI_API_KEY", ""), type="password")
    max_results = st.slider("Number of publications to analyze", 10, 100, 40, step=10)

field = st.selectbox("Field", list(FIELDS.keys()))
source = FIELDS[field][2]
term = st.text_input("Keyword or topic", placeholder="e.g. sepsis biomarkers children")
go = st.button("Analyze", type="primary")


def ai_missing():
    if provider == PROVIDERS[0] and not _load_chatgpt().get("access_token"):
        return "Click “Continue with ChatGPT” in the sidebar."
    if provider == PROVIDERS[1] and not claude_key:
        return "Enter your Claude API key in the sidebar."
    if provider == PROVIDERS[2] and not gemini_key:
        return "Enter your Gemini API key in the sidebar."
    if not model:
        return "Enter a model name in the sidebar."
    return None


def ask_ai(prompt):
    if provider == PROVIDERS[0]:
        return ask_chatgpt(prompt, model)
    if provider == PROVIDERS[1]:
        return ask_claude(prompt, claude_key, model)
    if provider == PROVIDERS[2]:
        return ask_gemini(prompt, gemini_key, model)
    return ask_ollama(prompt, ollama_url, model)


if go:
    if not term.strip():
        st.error("Enter a keyword.")
    elif ai_missing():
        st.error(ai_missing())
    elif source == "PubMed" and not email:
        st.error("Enter your email in the sidebar.")
    elif source == "OpenAlex" and not openalex_key:
        st.error("Enter your OpenAlex API key in the sidebar.")
    else:
        try:
            with st.spinner(f"Searching {source}..."):
                articles, total, must_read, source = get_literature(
                    term, field, max_results, email, ncbi_key, openalex_key
                )
            if not articles:
                st.warning(f"No {source} results. Try a broader keyword.")
                st.stop()
            st.info(f"{source} hits in total: {total} · analyzing {len(articles)} publications")

            with st.spinner("AI is analyzing the literature..."):
                result = ask_ai(build_prompt(term, articles, field))

            st.markdown(result)

            st.subheader("5. Must-read papers (most cited)")
            st.markdown(must_read_markdown(must_read))

            st.divider()
            st.subheader("Analyzed publications")
            for a in articles:
                with st.expander(f"{a['year']} – {a['title']}"):
                    st.markdown(f"[{a['id']}]({a['url']}) · {', '.join(a['types'])}")
                    st.write(a["abstract"] or "No abstract available.")
        except requests.HTTPError as e:
            st.error(f"{source} error: {e}")
        except Exception as e:
            st.error(f"Error: {e}")
