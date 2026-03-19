"""x402 payment-aware HTTP client for the Commune SDK.

Handles the x402 payment flow transparently:
  1. Makes the request (no Authorization header)
  2. Gets 402 Payment Required with payment details
  3. x402 client signs a USDC payment
  4. Retries the request with PAYMENT-SIGNATURE header

The developer creates and owns the x402 client — we never touch private keys.
Pass a pre-configured x402Client (with your own signers, networks, policies).

Requires optional dependencies: pip install x402[evm] eth-account
"""

from __future__ import annotations

from typing import Any

import httpx

from commune._http import HttpClient, DEFAULT_BASE_URL, _resolve_sdk_version
from commune.exceptions import CommuneError


class X402HttpClient(HttpClient):
    """HTTP client that pays for API calls via x402 (USDC).

    Accepts a pre-configured x402Client. We never handle private keys directly.
    The developer creates the client with their own signers and passes it in.

    Example::

        from x402 import x402Client
        from x402.mechanisms.evm.exact import ExactEvmScheme
        from eth_account import Account

        x402 = x402Client()
        x402.register("eip155:*", ExactEvmScheme(signer=Account.from_key("0x...")))

        from commune import CommuneClient
        client = CommuneClient(wallet=x402)
    """

    def __init__(
        self,
        wallet: object,
        base_url: str | None = None,
        timeout: float = 30.0,
    ):
        if not hasattr(wallet, 'create_payment_payload'):
            raise TypeError(
                "wallet must be a configured x402Client with a create_payment_payload method.\n"
                "Example:\n"
                "  from x402 import x402Client\n"
                "  from x402.mechanisms.evm.exact import ExactEvmScheme\n"
                "  from eth_account import Account\n\n"
                "  x402 = x402Client()\n"
                "  x402.register('eip155:*', ExactEvmScheme(signer=Account.from_key('0x...')))\n"
                "  client = CommuneClient(wallet=x402)"
            )

        self._base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self._x402_client = wallet

        # Base httpx client — no Authorization header (x402 handles auth)
        self._client = httpx.Client(
            base_url=self._base_url,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": f"commune-python/{_resolve_sdk_version()}",
            },
            timeout=timeout,
        )

    def _handle_402(
        self,
        response: httpx.Response,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> httpx.Response:
        """Handle a 402 Payment Required response by signing and retrying."""
        try:
            payment_required = response.json()
        except Exception:
            raise CommuneError("Invalid 402 response from server", status_code=402)

        accepts = payment_required.get("accepts", [])
        if not accepts:
            raise CommuneError(
                "Server returned 402 but no payment requirements",
                status_code=402,
            )

        try:
            payment_payload = self._x402_client.create_payment_payload(accepts)
        except Exception as e:
            raise CommuneError(
                f"Failed to create x402 payment: {e}",
                status_code=402,
            ) from e

        headers = dict(kwargs.get("headers", {}))
        headers["PAYMENT-SIGNATURE"] = payment_payload

        retry_kwargs = {k: v for k, v in kwargs.items() if k != "headers"}
        return self._client.request(method, path, headers=headers, **retry_kwargs)

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        unwrap_data: bool = True,
    ) -> Any:
        """Make a request, handling 402 payment flow transparently."""
        clean_params = {k: v for k, v in (params or {}).items() if v is not None} if params else None
        resp = self._client.request(method, path, params=clean_params or None, json=json)

        if resp.status_code == 402:
            resp = self._handle_402(resp, method, path, params=clean_params, json=json)

        return self._unwrap(resp, unwrap_data=unwrap_data)

    def get(self, path: str, params: dict[str, Any] | None = None, *, unwrap_data: bool = True) -> Any:
        return self._request("GET", path, params=params, unwrap_data=unwrap_data)

    def post(self, path: str, json: dict[str, Any] | None = None, *, unwrap_data: bool = True) -> Any:
        return self._request("POST", path, json=json, unwrap_data=unwrap_data)

    def put(self, path: str, json: dict[str, Any] | None = None, *, unwrap_data: bool = True) -> Any:
        return self._request("PUT", path, json=json, unwrap_data=unwrap_data)

    def delete(self, path: str, *, unwrap_data: bool = True) -> Any:
        return self._request("DELETE", path, unwrap_data=unwrap_data)
