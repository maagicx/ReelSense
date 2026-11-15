"""Pluggable sign-in: one FastMCP OAuth provider chosen by AUTH_PROVIDER, plus an
admin allowlist (ALLOWED_USERS) that works the same way whichever provider is used.

    google  Google accounts (default). ALLOWED_USERS required.
    github  GitHub accounts. ALLOWED_USERS required.
    oidc    Any OpenID Connect provider: Keycloak, Authentik, Zitadel, Pocket ID, Auth0...
            Access is usually managed there; ALLOWED_USERS is optional.
    entra   Microsoft Entra ID (one tenant). Access is managed in Entra; ALLOWED_USERS
            and ENTRA_ALLOWED_GROUP_ID are optional extras.

Every provider is an OAuth proxy, so MCP clients (claude.ai, ChatGPT, Claude Code) get
the dynamic client registration they need even when the upstream provider has none.
"""
import os

PROVIDER = (os.environ.get("AUTH_PROVIDER") or "google").strip().lower()
LABELS = {"google": "Google", "github": "GitHub", "oidc": "organisation",
          "entra": "Microsoft"}
LABEL = (os.environ.get("AUTH_PROVIDER_LABEL") or "").strip() or LABELS.get(PROVIDER, PROVIDER)


def _env(*names: str) -> str:
    """First non-empty of these variables (the ENTRA_* names still work for entra)."""
    for n in names:
        v = os.environ.get(n, "").strip()
        if v:
            return v
    return ""


CLIENT_ID = _env("AUTH_CLIENT_ID", "ENTRA_CLIENT_ID" if PROVIDER == "entra" else "")
CLIENT_SECRET = _env("AUTH_CLIENT_SECRET", "ENTRA_CLIENT_SECRET" if PROVIDER == "entra" else "")
ENTRA_TENANT_ID = _env("ENTRA_TENANT_ID")
ENTRA_ALLOWED_GROUP_ID = _env("ENTRA_ALLOWED_GROUP_ID")
OIDC_CONFIG_URL = _env("OIDC_CONFIG_URL")
OIDC_AUDIENCE = _env("OIDC_AUDIENCE")

# Comma-separated: emails (alice@example.com), whole domains (@example.com), GitHub
# usernames (octocat), or * for anyone who can sign in. Case doesn't matter.
ALLOWED_USERS = [u.strip().lower() for u in os.environ.get("ALLOWED_USERS", "").split(",") if u.strip()]


def config_errors() -> list[str]:
    """What is missing or wrong in the sign-in settings; empty when it's all there."""
    errs = []
    if PROVIDER not in LABELS:
        return [f"AUTH_PROVIDER={PROVIDER!r} is not one of: {', '.join(LABELS)}"]
    if not CLIENT_ID:
        errs.append("AUTH_CLIENT_ID is not set")
    if not CLIENT_SECRET:
        errs.append("AUTH_CLIENT_SECRET is not set")
    if PROVIDER == "entra" and not ENTRA_TENANT_ID:
        errs.append("ENTRA_TENANT_ID is not set")
    if PROVIDER == "oidc" and not OIDC_CONFIG_URL:
        errs.append("OIDC_CONFIG_URL is not set")
    if PROVIDER in ("google", "github") and not ALLOWED_USERS:
        # Anyone with a Google or GitHub account could otherwise use the server (and its
        # paid APIs). '*' is the explicit way to allow that.
        errs.append(f"ALLOWED_USERS is required with AUTH_PROVIDER={PROVIDER} "
                    "(emails, @domains or usernames; * allows anyone)")
    return errs


def build(base_url: str, jwt_signing_key: str, client_redirects: list[str]):
    """The FastMCP auth provider for AUTH_PROVIDER. Its callback is base_url/auth/callback."""
    common = dict(client_id=CLIENT_ID, client_secret=CLIENT_SECRET, base_url=base_url,
                  jwt_signing_key=jwt_signing_key, allowed_client_redirect_uris=client_redirects)
    if PROVIDER == "google":
        from fastmcp.server.auth.providers.google import GoogleProvider
        return GoogleProvider(required_scopes=["openid", "https://www.googleapis.com/auth/userinfo.email"],
                              **common)
    if PROVIDER == "github":
        from fastmcp.server.auth.providers.github import GitHubProvider
        return GitHubProvider(required_scopes=["read:user"], **common)  # read-only profile
    if PROVIDER == "oidc":
        from fastmcp.server.auth.oidc_proxy import OIDCProxy
        # Without an API audience the access token may not be a JWT this server can check,
        # so the OIDC id_token (always a signed JWT, carrying email) is verified instead.
        return OIDCProxy(config_url=OIDC_CONFIG_URL, required_scopes=["openid", "email", "profile"],
                         audience=OIDC_AUDIENCE or None, verify_id_token=not OIDC_AUDIENCE, **common)
    from fastmcp.server.auth.providers.azure import AzureProvider
    return AzureProvider(tenant_id=ENTRA_TENANT_ID, required_scopes=["access_as_user"], **common)


def _truthy(v) -> bool:
    return v is True or str(v).lower() == "true"


def identity(claims: dict) -> tuple[str, str]:
    """(stable user id for caps and job ownership, display name for logs and health)."""
    uid = claims.get("oid") or claims.get("sub") or "?"
    name = (claims.get("email") or claims.get("login") or claims.get("preferred_username")
            or claims.get("name") or uid)
    return f"{PROVIDER}:{uid}", name


def _names(claims: dict) -> set[str]:
    """Every name the allowlist may match for this user, lower case."""
    out = set()
    email = claims.get("email")
    # Google and OIDC say whether the address was verified; an unverified one could be anyone's.
    if email and (PROVIDER in ("github", "entra") or _truthy(claims.get("email_verified"))):
        out.add(email.lower())
    for k in ("login", "preferred_username"):  # GitHub username; Entra/OIDC sign-in name
        if claims.get(k):
            out.add(str(claims[k]).lower())
    return out


def is_allowed(claims: dict) -> bool:
    """True when ALLOWED_USERS (if set) and ENTRA_ALLOWED_GROUP_ID (if set) let this user in."""
    if PROVIDER == "entra" and ENTRA_ALLOWED_GROUP_ID and \
            ENTRA_ALLOWED_GROUP_ID not in (claims.get("groups") or []):
        return False
    if not ALLOWED_USERS or "*" in ALLOWED_USERS:
        return True
    names = _names(claims)
    for rule in ALLOWED_USERS:
        if rule.startswith("@"):
            if any(n.endswith(rule) for n in names if "@" in n):
                return True
        elif rule in names:
            return True
    return False


def gate_on() -> bool:
    """Whether a per-request check is needed at all."""
    return bool((ALLOWED_USERS and "*" not in ALLOWED_USERS)
                or (PROVIDER == "entra" and ENTRA_ALLOWED_GROUP_ID))
