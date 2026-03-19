"""x402 payment-aware HTTP client for the Commune SDK.

Handles the x402 payment flow transparently:
  1. Makes the request (no Authorization header)
  2. Gets 402 Payment Required with payment details
  3. Signs a USDC payment using the wallet's private key
  4. Retries the request with PAYMENT-SIGNATURE header

Supports two wallet modes:
  - str: private key → we create the signer (EVM/Base by default)
  - x402Client: pre-configured client → we use it directly

Requires optional dependencies: pip install commune[x402]
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from commune._http import HttpClient, DEFAULT_BASE_URL, _resolve_sdk_version
from commune.exceptions import CommuneError


class X402HttpClient(HttpClient):
    """HTTP client that pays for API calls via x402 (USDC on Base).

    Extends HttpClient but replaces Bearer token auth with x402 payment flow.
    The wallet private key never leaves the process — it's used in-memory
    to sign payment authorizations only.
    """

    def __init__(
        self,
        wallet: str | object,
        base_url: str | None = None,
        timeout: float = 30.0,
    ):
        self._base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self._timeout = timeout
        self._wallet = wallet
        self._x402_client = self._create_x402_client(wallet)

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

    @staticmethod
    def _create_x402_client(wallet: str | object) -> Any:
        """Create an x402 client from a private key or use an existing one."""
        if isinstance(wallet, str):
            try:
                from x402 import x402Client
                from x402.mechanisms.evm.exact import ExactEvmScheme
                from eth_account import Account
            except ImportError:
                raise ImportError(
                    "x402 wallet mode requires extra dependencies. Install them:\n"
                    "  pip install commune[x402]\n"
                    "  # or: pip install x402[evm] eth_account"
                ) from None

            key = wallet if wallet.startswith("0x") else f"0x{wallet}"
            signer = Account.from_key(key)
            client = x402Client()
            client.register("eip155:*", ExactEvmScheme(signer=signer))
            return client
        else:
            # Pre-configured x402Client — use it directly
            return wallet

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

        # Extract payment requirements (accepts array from the 402 body)
        accepts = payment_required.get("accepts", [])
        if not accepts:
            raise CommuneError(
                "Server returned 402 but no payment requirements",
                status_code=402,
            )

        # Use the x402 client to create a payment payload
        try:
            payment_payload = self._x402_client.create_payment_payload(accepts)
        except Exception as e:
            raise CommuneError(
                f"Failed to create x402 payment: {e}",
                status_code=402,
            ) from e

        # Retry the request with the payment signature
        headers = dict(kwargs.get("headers", {}))
        headers["PAYMENT-SIGNATURE"] = payment_payload

        retry_kwargs = {k: v for k, v in kwargs.items() if k != "headers"}
        retry_resp = self._client.request(method, path, headers=headers, **retry_kwargs)
        return retry_resp

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

        # If 402, pay and retry
        if resp.status_code == 402:
            resp = self._handle_402(resp, method, path, params=clean_params, json=json)

        return self._unwrap(resp, unwrap_data=unwrap_data)

    # Override base class methods to use _request with 402 handling

    def get(self, path: str, params: dict[str, Any] | None = None, *, unwrap_data: bool = True) -> Any:
        return self._request("GET", path, params=params, unwrap_data=unwrap_data)

    def post(self, path: str, json: dict[str, Any] | None = None, *, unwrap_data: bool = True) -> Any:
        return self._request("POST", path, json=json, unwrap_data=unwrap_data)

    def put(self, path: str, json: dict[str, Any] | None = None, *, unwrap_data: bool = True) -> Any:
        return self._request("PUT", path, json=json, unwrap_data=unwrap_data)

    def delete(self, path: str, *, unwrap_data: bool = True) -> Any:
        return self._request("DELETE", path, unwrap_data=unwrap_data)
