"""Domain data models and value objects for iShop (Drake).

Zero external network/provider dependencies.
Strictly adheres to Safety S-01, S-02, S-03, S-08, S-11.
"""

from __future__ import annotations

import hashlib
import hmac
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from typing import Any, Mapping

SCHEMA_VERSION = "1.0.0"

# ISO 4217 currency decimal exponent lookup
CURRENCY_DECIMALS: dict[str, int] = {
    "USD": 2,
    "EUR": 2,
    "GBP": 2,
    "NGN": 2,
    "CAD": 2,
    "AUD": 2,
    "JPY": 0,
    "KRW": 0,
    "KWD": 3,
    "BHD": 3,
    "OMR": 3,
}


@dataclass(frozen=True)
class Money:
    """Exact currency and money representation.

    Prevents floating-point arithmetic errors by performing operations on
    Decimal and integer minor units based on ISO currency metadata.
    """

    amount: Decimal
    currency: str

    def __post_init__(self):
        curr = self.currency.upper()
        object.__setattr__(self, "currency", curr)
        if not curr.isalpha() or len(curr) != 3:
            raise ValueError(f"Invalid ISO currency code: {self.currency}")

    @property
    def decimals(self) -> int:
        return CURRENCY_DECIMALS.get(self.currency, 2)

    @classmethod
    def from_string(cls, amount_str: str, currency: str) -> Money:
        dec = Decimal(str(amount_str).strip())
        return cls(amount=dec, currency=currency)

    @classmethod
    def from_minor_units(cls, minor_units: int, currency: str) -> Money:
        curr = currency.upper()
        decimals = CURRENCY_DECIMALS.get(curr, 2)
        factor = Decimal(10**decimals)
        amount = Decimal(minor_units) / factor
        return cls(amount=amount, currency=curr)

    def to_minor_units(self) -> int:
        factor = Decimal(10**self.decimals)
        quantized = (self.amount * factor).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        return int(quantized)

    def __add__(self, other: Money) -> Money:
        if not isinstance(other, Money) or self.currency != other.currency:
            raise ValueError(f"Cannot add mismatched currencies: {self.currency} and {getattr(other, 'currency', None)}")
        return Money(amount=self.amount + other.amount, currency=self.currency)

    def __mul__(self, quantity: int) -> Money:
        if type(quantity) is not int or quantity < 0:
            raise ValueError("Money quantity must be a nonnegative integer")
        return Money(amount=self.amount * quantity, currency=self.currency)

    def __sub__(self, other: Money) -> Money:
        if not isinstance(other, Money) or self.currency != other.currency:
            raise ValueError(f"Cannot subtract mismatched currencies: {self.currency} and {getattr(other, 'currency', None)}")
        return Money(amount=self.amount - other.amount, currency=self.currency)

    def __lt__(self, other: Money) -> bool:
        if not isinstance(other, Money) or self.currency != other.currency:
            raise ValueError(f"Cannot compare mismatched currencies: {self.currency} and {getattr(other, 'currency', None)}")
        return self.amount < other.amount

    def __le__(self, other: Money) -> bool:
        return self == other or self < other

    def __str__(self) -> str:
        fmt = f"{{:.{self.decimals}f}}"
        return f"{fmt.format(self.amount)} {self.currency}"


@dataclass(frozen=True)
class CartLine:
    """A single canonical cart line.

    Identity key is based on (variant_id, selling_plan_id, normalized_properties).
    Multiple lines with different properties or subscription plans remain distinct.
    shopify_line_key preserves the ephemeral Storefront API/theme line locator (R5).
    """

    variant_id: str
    quantity: int
    selling_plan_id: str | None = None
    properties: dict[str, str] = field(default_factory=dict)
    shopify_line_key: str | None = None

    def __post_init__(self):
        if self.quantity < 0:
            raise ValueError(f"Line quantity cannot be negative: {self.quantity}")
        # Normalize properties by sorting keys and converting values to string
        norm_props = {str(k): str(v) for k, v in sorted(self.properties.items())}
        object.__setattr__(self, "properties", norm_props)

    @property
    def canonical_key(self) -> str:
        plan_part = self.selling_plan_id if self.selling_plan_id else "none"
        props_part = "&".join(f"{k}={v}" for k, v in self.properties.items())
        return f"{self.variant_id}::{plan_part}::{props_part}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "variant_id": self.variant_id,
            "quantity": self.quantity,
            "selling_plan_id": self.selling_plan_id,
            "properties": dict(self.properties),
            "shopify_line_key": self.shopify_line_key,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CartLine:
        return cls(
            variant_id=str(data["variant_id"]),
            quantity=int(data["quantity"]),
            selling_plan_id=data.get("selling_plan_id"),
            properties=data.get("properties") or {},
            shopify_line_key=data.get("shopify_line_key"),
        )


@dataclass(frozen=True)
class CartSnapshot:
    """Authoritative snapshot of shopper's browser cart.

    Multiset equivalence checks ignore line display order and ephemeral keys,
    preserving Safety S-08 (preserve unrelated cart contents).
    """

    shop_id: str
    currency: str
    lines: tuple[CartLine, ...] = ()
    cart_token_hash: str | None = None  # Keyed hash only, never raw token

    def multiset_counter(self) -> Counter[str]:
        counter: Counter[str] = Counter()
        for line in self.lines:
            counter[line.canonical_key] += line.quantity
        return counter

    def fingerprint(self) -> str:
        """Deterministic sha256 fingerprint of canonical lines multiset."""
        counter = self.multiset_counter()
        sorted_elements = sorted(counter.items())
        serialized = ";".join(f"{key}*x{qty}" for key, qty in sorted_elements)
        content = f"{self.shop_id}|{self.currency}|{serialized}"
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def is_equivalent(self, other: Any) -> bool:
        if not isinstance(other, CartSnapshot):
            return False
        if self.shop_id != other.shop_id or self.currency != other.currency:
            return False
        return self.multiset_counter() == other.multiset_counter()

    def to_dict(self) -> dict[str, Any]:
        return {
            "shop_id": self.shop_id,
            "currency": self.currency,
            "lines": [line.to_dict() for line in self.lines],
            "cart_token_hash": self.cart_token_hash,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CartSnapshot:
        return cls(
            shop_id=str(data["shop_id"]),
            currency=str(data["currency"]),
            lines=tuple(CartLine.from_dict(l) for l in data.get("lines", [])),
            cart_token_hash=data.get("cart_token_hash"),
        )


class CommandOperation(str, Enum):
    """Closed union of permitted storefront operations (Safety S-01, S-02).

    Strictly excludes payment, card submission, order placement, or arbitrary scripts.
    """

    SEARCH_CATALOG = "search_catalog"
    BROWSE_STORE = "browse_store"
    GET_PRODUCT = "get_product"
    SHOW_VARIANT = "show_variant"
    READ_CART = "read_cart"
    ADD_VARIANT = "add_variant"
    SET_LINE_QUANTITY = "set_line_quantity"
    REMOVE_LINE = "remove_line"
    NAVIGATE_STOREFRONT = "navigate_storefront"
    HANDOFF_TO_CHECKOUT = "handoff_to_checkout"


FORBIDDEN_COMMAND_NAMES = {
    "payment",
    "pay",
    "charge",
    "submit_card",
    "place_order",
    "execute_script",
    "eval",
    "arbitrary_url",
    "clear_cart",
    "manage_orders",
}


@dataclass(frozen=True)
class AuthorizedCommand:
    """Internal command envelope minted only by backend verification gate."""

    command_id: str
    session_id: str
    shop_id: str
    turn_id: str
    request_revision: int
    page_epoch: int
    expires_at_ms: int
    operation: CommandOperation
    parameters: Mapping[str, Any]
    expected_cart_fingerprint: str | None = None
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self):
        if not self.command_id.startswith("cmd_"):
            raise ValueError(f"Invalid command ID format: {self.command_id}")
        if self.operation.value in FORBIDDEN_COMMAND_NAMES:
            raise ValueError(f"Safety S-01 violation: Forbidden operation '{self.operation}'")
        if self.request_revision < 1:
            raise ValueError("request_revision must be >= 1")
        if self.page_epoch < 1:
            raise ValueError("page_epoch must be >= 1")


@dataclass(frozen=True)
class SessionGrant:
    """Short-lived signed shopper grant for authenticating runtime connections (Safety S-11)."""

    grant_id: str
    shop_id: str
    permitted_origin: str
    anonymous_session_id: str
    config_revision: str
    issued_at_ms: int
    expires_at_ms: int
    signature: str
    schema_version: str = SCHEMA_VERSION

    @classmethod
    def create_signed(
        cls,
        grant_id: str,
        shop_id: str,
        permitted_origin: str,
        anonymous_session_id: str,
        config_revision: str,
        issued_at_ms: int,
        ttl_ms: int,
        signing_secret: str,
    ) -> SessionGrant:
        expires_at_ms = issued_at_ms + ttl_ms
        msg = f"{grant_id}|{shop_id}|{permitted_origin}|{anonymous_session_id}|{config_revision}|{issued_at_ms}|{expires_at_ms}"
        sig = hmac.new(signing_secret.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256).hexdigest()
        return cls(
            grant_id=grant_id,
            shop_id=shop_id,
            permitted_origin=permitted_origin,
            anonymous_session_id=anonymous_session_id,
            config_revision=config_revision,
            issued_at_ms=issued_at_ms,
            expires_at_ms=expires_at_ms,
            signature=sig,
        )

    def is_valid_at(self, current_time_ms: int) -> bool:
        return self.issued_at_ms <= current_time_ms <= self.expires_at_ms

    def verify_signature(self, signing_secret: str) -> bool:
        msg = f"{self.grant_id}|{self.shop_id}|{self.permitted_origin}|{self.anonymous_session_id}|{self.config_revision}|{self.issued_at_ms}|{self.expires_at_ms}"
        expected_sig = hmac.new(signing_secret.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256).hexdigest()
        return hmac.compare_digest(self.signature, expected_sig)

    def verify_tenancy(self, expected_shop_id: str, request_origin: str) -> bool:
        """Enforces shop and origin binding (T-15, T-22)."""
        return (self.shop_id == expected_shop_id) and (self.permitted_origin == request_origin)
