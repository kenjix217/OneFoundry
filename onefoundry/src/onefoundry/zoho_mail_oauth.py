"""Zoho-discovered OAuth with PKCE, resource binding and bounded vendor-only I/O."""

from __future__ import annotations

import json
import re
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Protocol
from urllib.parse import urlencode, urlsplit

import httpx

from onefoundry.connector_oauth import pkce_pair
from onefoundry.zoho_mail_contracts import endpoint
from onefoundry.zoho_mail_privacy import private_call


class ZohoOAuthError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class OAuthTransport(Protocol):
    def __call__(
        self, method: str, url: str, *, body: dict[str, Any] | None = None, form: bool = False
    ) -> tuple[int, dict[str, Any], dict[str, str]]: ...


@contextmanager
def private_oauth() -> Iterator[None]:
    marker = private_call.set(True)
    try:
        yield
    finally:
        private_call.reset(marker)


class BoundedOAuthHTTP:
    def __init__(self, timeout: float, max_bytes: int) -> None:
        self.timeout, self.max_bytes = timeout, max_bytes

    def __call__(
        self, method: str, url: str, *, body: dict[str, Any] | None = None, form: bool = False
    ) -> tuple[int, dict[str, Any], dict[str, str]]:
        try:
            with private_oauth(), httpx.Client(timeout=self.timeout, follow_redirects=False) as client:
                kwargs: dict[str, Any] = {"headers": {"Accept": "application/json, text/event-stream"}}
                if body is not None:
                    kwargs["data" if form else "json"] = body
                with client.stream(method, url, **kwargs) as response:
                    if 300 <= response.status_code < 400:
                        raise ZohoOAuthError("oauth_redirect_rejected")
                    chunks: list[bytes] = []
                    size = 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > self.max_bytes:
                            raise ZohoOAuthError("oauth_response_too_large")
                        chunks.append(chunk)
                    raw = b"".join(chunks)
                    # OAuth's initial 401 challenge can have a plain-text body.
                    # Discovery uses its authenticated resource header, not that body.
                    value = json.loads(raw) if raw and response.status_code in {200, 201} else {}
                    if not isinstance(value, dict):
                        raise ZohoOAuthError("oauth_invalid_response")
                    return response.status_code, value, dict(response.headers)
        except ZohoOAuthError:
            raise
        except Exception:
            raise ZohoOAuthError("oauth_transport_failed") from None


class ZohoOAuth:
    def __init__(self, http: OAuthTransport, authority_hosts: tuple[str, ...]) -> None:
        self.http, self.authority_hosts = http, authority_hosts

    def trusted(self, url: str, server_url: str) -> str:
        parsed, server = urlsplit(url), urlsplit(endpoint(server_url))
        if (
            parsed.scheme != "https"
            or parsed.netloc != parsed.hostname
            or parsed.query
            or parsed.fragment
            or not parsed.hostname
            or (parsed.hostname != server.hostname and parsed.hostname not in self.authority_hosts)
        ):
            raise ZohoOAuthError("oauth_authority_rejected")
        return url

    def _json(
        self,
        method: str,
        url: str,
        server_url: str,
        *,
        body: dict[str, Any] | None = None,
        form: bool = False,
    ) -> dict[str, Any]:
        self.trusted(url, server_url)
        status, data, _ = self.http(method, url, body=body, form=form)
        if status not in {200, 201}:
            raise ZohoOAuthError("oauth_request_rejected")
        return data

    def discover(self, server_url: str) -> dict[str, Any]:
        server_url = endpoint(server_url)
        status, _, headers = self.http(
            "POST",
            server_url,
            body={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "OneFoundry", "version": "1"},
                },
            },
        )
        challenge = re.search(r'resource_metadata="([^"]+)"', headers.get("www-authenticate", ""))
        if status != 401 or challenge is None:
            raise ZohoOAuthError("oauth_discovery_unavailable")
        resource_url = challenge[1]
        if urlsplit(resource_url).netloc != urlsplit(server_url).netloc:
            raise ZohoOAuthError("oauth_resource_rejected")
        resource = self._json("GET", resource_url, server_url)
        servers = resource.get("authorization_servers")
        if resource.get("resource") != server_url or not isinstance(servers, list) or len(servers) != 1:
            raise ZohoOAuthError("oauth_resource_rejected")
        issuer = self.trusted(str(servers[0]), server_url)
        parsed = urlsplit(issuer)
        metadata_url = (
            f"https://{parsed.netloc}/.well-known/oauth-authorization-server{parsed.path.rstrip('/')}"
        )
        metadata = self._json("GET", metadata_url, server_url)
        if metadata.get("issuer") != issuer or "S256" not in metadata.get(
            "code_challenge_methods_supported", []
        ):
            raise ZohoOAuthError("oauth_pkce_required")
        scopes = resource.get("scopes_supported") or metadata.get("scopes_supported")
        if (
            not isinstance(scopes, list)
            or not scopes
            or any(
                not isinstance(scope, str) or not scope or any(char.isspace() for char in scope)
                for scope in scopes
            )
        ):
            raise ZohoOAuthError("oauth_scopes_unavailable")
        result = {
            "issuer": issuer,
            "scope": " ".join(scopes),
            "resource": server_url,
            "issuer_required": metadata.get("authorization_response_iss_parameter_supported") is True,
        }
        for key in ("authorization_endpoint", "token_endpoint", "registration_endpoint"):
            result[key] = self.trusted(str(metadata.get(key, "")), server_url)
        if "none" not in metadata.get("token_endpoint_auth_methods_supported", ["client_secret_basic"]):
            raise ZohoOAuthError("oauth_public_client_required")
        result["refresh_supported"] = "refresh_token" in metadata.get("grant_types_supported", [])
        return result

    def register(self, server_url: str, metadata: dict[str, Any], redirect_uri: str) -> dict[str, Any]:
        data = self._json(
            "POST",
            metadata["registration_endpoint"],
            server_url,
            body={
                "client_name": "OneFoundry",
                "redirect_uris": [redirect_uri],
                "response_types": ["code"],
                "grant_types": ["authorization_code"]
                + (["refresh_token"] if metadata["refresh_supported"] else []),
                "token_endpoint_auth_method": "none",
                "scope": metadata["scope"],
            },
        )
        client_id = data.get("client_id")
        if (
            not isinstance(client_id, str)
            or not client_id
            or data.get("token_endpoint_auth_method", "none") != "none"
        ):
            raise ZohoOAuthError("oauth_client_registration_failed")
        if data.get("redirect_uris") != [redirect_uri]:
            raise ZohoOAuthError("oauth_redirect_uri_mismatch")
        return {"client_id": client_id, "redirect_uri": redirect_uri}

    def pending(self, metadata: dict[str, Any], client: dict[str, Any], expires_at: float) -> dict[str, Any]:
        verifier, challenge = pkce_pair()
        state = secrets.token_urlsafe(32)
        params = {
            "client_id": client["client_id"],
            "redirect_uri": client["redirect_uri"],
            "response_type": "code",
            "scope": metadata["scope"],
            "resource": metadata["resource"],
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        return {
            "phase": "waiting",
            "state": state,
            "verifier": verifier,
            "expires_at": expires_at,
            "authorize_url": metadata["authorization_endpoint"] + "?" + urlencode(params),
        }

    def tokens(
        self, metadata: dict[str, Any], client: dict[str, Any], body: dict[str, str]
    ) -> dict[str, Any]:
        data = self._json(
            "POST",
            metadata["token_endpoint"],
            metadata["resource"],
            form=True,
            body={"client_id": client["client_id"], "resource": metadata["resource"], **body},
        )
        access, kind, expiry = data.get("access_token"), data.get("token_type"), data.get("expires_in")
        if (
            not isinstance(access, str)
            or not access
            or str(kind).lower() != "bearer"
            or any(ord(char) < 33 or ord(char) > 126 for char in access)
        ):
            raise ZohoOAuthError("oauth_invalid_token")
        if expiry is not None and (type(expiry) is not int or expiry <= 0):
            raise ZohoOAuthError("oauth_invalid_token")
        scope = data.get("scope", metadata["scope"])
        if not isinstance(scope, str) or set(scope.split()) != set(metadata["scope"].split()):
            raise ZohoOAuthError("oauth_scope_changed")
        refresh = data.get("refresh_token")
        if refresh is not None and (
            not isinstance(refresh, str)
            or not refresh
            or any(ord(char) < 33 or ord(char) > 126 for char in refresh)
        ):
            raise ZohoOAuthError("oauth_invalid_token")
        return {"access_token": access, "refresh_token": refresh, "expires_in": expiry, "scope": scope}
