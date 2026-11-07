"""Branded OAuth consent page ("Allow Claude to use ReelSense?").

FastMCP's OAuth proxy draws its consent page with oauth_proxy.ui.create_consent_html and
has no theming hook, so install() swaps in this one, called with the same keyword
arguments. What makes the page safe stays as FastMCP has it: the same form fields
(txn_id, csrf_token, action) posting back to the same URL, the callback address shown
up front, the verified-domain badge, and the same Content Security Policy.
"""
import html
import os

from signin import LABEL as SIGNIN_LABEL  # "Google", "GitHub", "Microsoft"... from AUTH_PROVIDER

OWNER = os.environ.get("SERVER_OWNER", "").strip() or "the server owner"
SERVER_TITLE = "ReelSense"

# Claude's warm palette: ivory and slate with the clay accent; dark mode follows the OS.
STYLES = """
:root {
  --bg: #f5f4ed; --card: #faf9f5; --fg: #141413; --muted: #6b6a63; --line: #e3e0d4;
  --accent: #d97757; --accent-hover: #c6613f; --accent-fg: #ffffff;
  --soft: #f0eee6; --ok-bg: #eef3e8; --ok-line: #c5d6b4; --ok-fg: #3d5a2a;
  --warn-bg: #fbf2e9; --warn-line: #ebcfb4;
  color-scheme: light;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #1f1e1d; --card: #262624; --fg: #f5f4ed; --muted: #a6a39a; --line: #3a3936;
    --accent: #d97757; --accent-hover: #e08a6d; --accent-fg: #ffffff;
    --soft: #30302e; --ok-bg: #263020; --ok-line: #3f5232; --ok-fg: #b9d3a3;
    --warn-bg: #33281f; --warn-line: #5a4330;
    color-scheme: dark;
  }
}
* { box-sizing: border-box; }
html, body { margin: 0; background: var(--bg); color: var(--fg);
  font: 15px/1.5 ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
main { max-width: 480px; margin: 48px auto; padding: 0 16px; }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 16px;
  padding: 32px 28px 24px; box-shadow: 0 1px 2px rgba(20, 20, 19, .04), 0 8px 24px rgba(20, 20, 19, .06); }
.brand { display: flex; align-items: center; gap: 12px; margin-bottom: 24px; }
.brand img { width: 44px; height: 44px; border-radius: 10px; }
.brand .name { font-weight: 600; font-size: 16px; margin: 0; }
.brand .by { color: var(--muted); font-size: 13px; margin: 0; }
h1 { font-family: ui-serif, Georgia, "Times New Roman", serif; font-weight: 500; font-size: 26px;
  line-height: 1.25; margin: 0 0 12px; letter-spacing: -0.01em; }
.lead { margin: 0 0 20px; color: var(--fg); }
.lead strong { font-weight: 600; }
.box { border-radius: 10px; padding: 12px 14px; margin-bottom: 12px; font-size: 14px; }
.verified { background: var(--ok-bg); border: 1px solid var(--ok-line); color: var(--ok-fg); }
.callback { background: var(--warn-bg); border: 1px solid var(--warn-line); }
.callback .label { display: block; color: var(--muted); font-size: 13px; margin-bottom: 4px; }
.callback code, .details code { font: 13px/1.45 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  overflow-wrap: anywhere; }
details { margin: 16px 0 4px; }
summary { cursor: pointer; color: var(--muted); font-size: 14px; }
.details { background: var(--soft); border-radius: 10px; padding: 4px 14px; margin-top: 8px; }
.row { display: grid; grid-template-columns: 9.5em 1fr; gap: 8px; padding: 8px 0; font-size: 13px;
  border-bottom: 1px solid var(--line); }
.row:last-child { border-bottom: 0; }
.row .k { color: var(--muted); }
.buttons { display: flex; gap: 10px; margin-top: 24px; }
button { flex: 1; font: inherit; font-weight: 600; border-radius: 10px; padding: 11px 16px; cursor: pointer;
  border: 1px solid transparent; }
.approve { background: var(--accent); color: var(--accent-fg); }
.approve:hover { background: var(--accent-hover); }
.deny { background: transparent; color: var(--fg); border-color: var(--line); }
.deny:hover { background: var(--soft); }
button:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.foot { color: var(--muted); font-size: 12.5px; text-align: center; margin: 16px 8px 0; }
.foot a { color: inherit; }
@media (max-width: 420px) { .card { padding: 24px 18px 18px; } .row { grid-template-columns: 1fr; gap: 0; } }
"""

DEFAULT_CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src https: data:; base-uri 'none'"


def consent_html(*, client_id, redirect_uri, scopes, txn_id, csrf_token, client_name=None,
                 server_name=None, server_icon_url=None, server_website_url=None,
                 client_website_url=None, csp_policy=None, is_cimd_client=False,
                 cimd_domain=None, **_ignored) -> str:
    """Same keyword arguments as FastMCP's create_consent_html (title is ignored)."""
    e = html.escape
    client = e(client_name or client_id)
    callback = e(redirect_uri)
    scope_text = ", ".join(e(s) for s in scopes) if scopes else "None"
    icon = (f'<img src="{e(server_icon_url)}" alt="">' if server_icon_url else "")
    site = (f' · <a href="{e(server_website_url)}" target="_blank" rel="noopener noreferrer">'
            f"{e(server_website_url.split('://')[-1].rstrip('/'))}</a>") if server_website_url else ""
    verified = (f'<div class="box verified">&#x2713; Verified domain: <strong>{e(cimd_domain)}</strong></div>'
                if is_cimd_client and cimd_domain else "")
    rows = [("Application", client), ("Website", e(client_website_url or "N/A")),
            ("Application ID", f"<code>{e(client_id)}</code>"), ("Redirect URI", f"<code>{callback}</code>"),
            ("Scopes", f"<code>{scope_text}</code>"), ("MCP server", e(server_name or "transcribe"))]
    rows_html = "".join(f'<div class="row"><span class="k">{k}</span><span>{v}</span></div>' for k, v in rows)
    csp = DEFAULT_CSP if csp_policy is None else csp_policy
    csp_meta = f'<meta http-equiv="Content-Security-Policy" content="{e(csp)}">' if csp else ""
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
{csp_meta}
<title>Allow {client} to use {SERVER_TITLE}?</title>
<style>{STYLES}</style>
</head>
<body>
<main>
  <div class="card">
    <div class="brand">{icon}<div><p class="name">{SERVER_TITLE}</p><p class="by">by {OWNER}{site}</p></div></div>
    <h1>Allow {client} to use {SERVER_TITLE}?</h1>
    <p class="lead"><strong>{client}</strong> is asking to use this server's video tools (transcripts, frames,
      music, video and channel details) as you. Only allow it if you started this from {client}.</p>
    {verified}
    <div class="box callback"><span class="label">Your sign-in will be sent to</span><code>{callback}</code></div>
    <details>
      <summary>Details</summary>
      <div class="details">{rows_html}</div>
    </details>
    <form method="POST" action="">
      <input type="hidden" name="txn_id" value="{e(txn_id)}">
      <input type="hidden" name="csrf_token" value="{e(csrf_token)}">
      <input type="hidden" name="submit" value="true">
      <div class="buttons">
        <button type="submit" name="action" value="approve" class="approve">Allow</button>
        <button type="submit" name="action" value="deny" class="deny">Deny</button>
      </div>
    </form>
  </div>
  <p class="foot">{SERVER_TITLE} is a private server run by {OWNER}, not by Anthropic. Next you sign in
    with your {SIGNIN_LABEL} account; access is granted by {OWNER}.</p>
</main>
</body>
</html>"""


def install() -> None:
    """Point FastMCP's consent handler at consent_html. It imports create_consent_html by
    name into its own module, so that is the reference to replace."""
    from fastmcp.server.auth.oauth_proxy import consent
    if not hasattr(consent, "create_consent_html"):
        raise RuntimeError("FastMCP's consent module changed; update consent_page.install().")
    consent.create_consent_html = consent_html
