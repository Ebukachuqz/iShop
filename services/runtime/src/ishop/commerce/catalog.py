"""Catalog and variant resolution for iShop (Drake).

Enforces Safety invariants:
- S-03: Facts come from Shopify evidence; reject invented product IDs or wrong currency (T-02).
- S-04: No silent variant substitution; incomplete options require clarification (T-03).
- T-07: Sold out variants and shortages reject false in-stock promises; unknown stock stays unknown.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping

from ishop.domain.models import Money


class ResolutionStatus(str, Enum):
    RESOLVED = "resolved"
    CLARIFY = "clarify"
    REFRESH = "refresh"
    REJECT = "reject"


@dataclass(frozen=True)
class VariantEvidence:
    """Observed variant fact from Storefront API or WebMCP."""

    variant_id: str
    product_id: str
    product_title: str
    variant_title: str
    selected_options: dict[str, str]  # e.g. {"Color": "Red", "Size": "Large"}
    price: Money
    available_for_sale: bool
    quantity_available: int | None = None  # None indicates unknown on-hand stock (T-07)
    inventory_policy: str = "DENY"  # "DENY" or "CONTINUE" (backorder allowed)

    def __post_init__(self):
        norm_opts = {str(k).lower(): str(v).lower() for k, v in self.selected_options.items()}
        object.__setattr__(self, "selected_options", norm_opts)


@dataclass(frozen=True)
class ProductEvidence:
    """Observed product fact from Storefront evidence."""

    product_id: str
    title: str
    variants: tuple[VariantEvidence, ...]
    options: tuple[str, ...] = ()  # e.g. ("Color", "Size")
    url: str | None = None


@dataclass(frozen=True)
class EvidenceSnapshot:
    """Point-in-time catalog evidence snapshot bound to shop and currency."""

    snapshot_id: str
    shop_id: str
    currency: str
    observed_at_ms: int
    products: dict[str, ProductEvidence] = field(default_factory=dict)
    query: str | None = None


def product_title_matches(query: str, title: str) -> bool:
    ignored = {"a", "an", "the"}
    query_tokens = {token for token in re.findall(r"[a-z0-9]+", query.lower()) if token not in ignored}
    title_tokens = {token for token in re.findall(r"[a-z0-9]+", title.lower()) if token not in ignored}
    return bool(query_tokens and title_tokens and (query_tokens <= title_tokens or title_tokens <= query_tokens))


@dataclass(frozen=True)
class IntentTarget:
    """Target reference requested by shopper or interpreted by LLM."""

    product_id: str | None = None
    title_query: str | None = None
    selected_options: dict[str, str] = field(default_factory=dict)
    excluded_options: dict[str, str] = field(default_factory=dict)
    quantity: int = 1

    def __post_init__(self):
        if self.quantity < 1:
            raise ValueError(f"Target quantity must be >= 1, got {self.quantity}")
        norm_opts = {str(k).lower(): str(v).lower() for k, v in self.selected_options.items()}
        norm_ex = {str(k).lower(): str(v).lower() for k, v in self.excluded_options.items()}
        object.__setattr__(self, "selected_options", norm_opts)
        object.__setattr__(self, "excluded_options", norm_ex)


@dataclass(frozen=True)
class ResolutionResult:
    """Outcome of resolving target intent against evidence snapshot."""

    status: ResolutionStatus
    variant: VariantEvidence | None = None
    product: ProductEvidence | None = None
    candidate_variants: tuple[VariantEvidence, ...] = ()
    missing_options: tuple[str, ...] = ()
    reason: str | None = None
    stock_known: bool = True


def _normalize_opt_val(key: str, val: str | None) -> set[str]:
    if not val:
        return set()
    k = key.lower().strip()
    v = str(val).lower().strip()
    syns = {v}
    if k == "size":
        if v in ("s", "small"):
            syns.update({"s", "small"})
        elif v in ("m", "medium", "med"):
            syns.update({"m", "medium", "med"})
        elif v in ("l", "large"):
            syns.update({"l", "large"})
        elif v in ("xl", "extra large"):
            syns.update({"xl", "extra large"})
    return syns


class CatalogResolver:
    """Deterministic catalog resolver without model shortcuts."""

    @staticmethod
    def resolve(
        evidence: EvidenceSnapshot,
        target: IntentTarget,
        expected_shop_id: str | None = None,
        expected_currency: str | None = None,
    ) -> ResolutionResult:
        # 1. Shop and currency evidence binding check (T-02, S-03)
        if expected_shop_id and evidence.shop_id != expected_shop_id:
            return ResolutionResult(
                status=ResolutionStatus.REJECT,
                reason=f"Evidence shop mismatch: expected '{expected_shop_id}', got '{evidence.shop_id}'",
            )
        if expected_currency and evidence.currency != expected_currency:
            return ResolutionResult(
                status=ResolutionStatus.REJECT,
                reason=f"Evidence currency mismatch: expected '{expected_currency}', got '{evidence.currency}'",
            )

        # 2. Product lookup (T-02)
        product: ProductEvidence | None = None
        if target.product_id:
            product = evidence.products.get(target.product_id)
            if not product:
                return ResolutionResult(
                    status=ResolutionStatus.REJECT,
                    reason=f"Invented or nonexistent product ID '{target.product_id}' (T-02)",
                )
        elif target.title_query:
            matching_products = [
                p for p in evidence.products.values()
                if product_title_matches(target.title_query, p.title)
            ]
            if not matching_products:
                return ResolutionResult(
                    status=ResolutionStatus.REJECT,
                    reason=f"No products in catalog matching query '{target.title_query}'",
                )
            if len(matching_products) > 1:
                # Ambiguous product selection requires clarification (T-03)
                all_candidates: list[VariantEvidence] = []
                for p in matching_products:
                    all_candidates.extend(p.variants)
                return ResolutionResult(
                    status=ResolutionStatus.CLARIFY,
                    candidate_variants=tuple(all_candidates[:5]),
                    reason=f"Multiple products match '{target.title_query}'; clarification required (T-03)",
                )
            product = matching_products[0]
        else:
            return ResolutionResult(
                status=ResolutionStatus.REJECT,
                reason="Target specification must supply product_id or title_query",
            )
        # 3. Option matching and variant selection (T-03, S-04)
        target_opts = target.selected_options
        matching_variants: list[VariantEvidence] = []

        # Check if requested options are explicitly unsupported
        for opt_key, opt_val in target_opts.items():
            valid_vals_for_opt = {
                v.selected_options.get(opt_key)
                for v in product.variants
                if opt_key in v.selected_options
            }
            req_syns = _normalize_opt_val(opt_key, opt_val)
            matched_valid = any(
                bool(req_syns & _normalize_opt_val(opt_key, valid_val))
                for valid_val in valid_vals_for_opt
                if valid_val
            )
            if valid_vals_for_opt and not matched_valid:
                # Explicit requested option does not exist on product.
                # S-04: Never silently substitute!
                return ResolutionResult(
                    status=ResolutionStatus.REJECT,
                    product=product,
                    candidate_variants=product.variants,
                    reason=f"Requested option '{opt_key}={opt_val}' is not available for '{product.title}'. Silent substitution forbidden (T-03, S-04).",
                )

        for variant in product.variants:
            matches_all = True
            for opt_key, opt_val in target_opts.items():
                var_val = variant.selected_options.get(opt_key)
                if not var_val:
                    matches_all = False
                    break
                req_syns = _normalize_opt_val(opt_key, opt_val)
                var_syns = _normalize_opt_val(opt_key, var_val)
                if not (req_syns & var_syns):
                    matches_all = False
                    break
            if matches_all and target.excluded_options:
                for ex_key, ex_val in target.excluded_options.items():
                    var_val = variant.selected_options.get(ex_key)
                    if var_val:
                        ex_syns = _normalize_opt_val(ex_key, ex_val)
                        var_syns = _normalize_opt_val(ex_key, var_val)
                        if ex_syns & var_syns:
                            matches_all = False
                            break
            if matches_all:
                matching_variants.append(variant)

        if not matching_variants:
            return ResolutionResult(
                status=ResolutionStatus.REJECT,
                product=product,
                reason=f"No variant found for '{product.title}' matching options {target_opts}",
            )

        if len(matching_variants) > 1:
            # Multiple variants match because some options are not specified (e.g. Size missing)
            # Find which options differ among matching variants
            distinct_opts: set[str] = set()
            for v in matching_variants:
                for k in v.selected_options:
                    if k not in target_opts:
                        distinct_opts.add(k)
            return ResolutionResult(
                status=ResolutionStatus.CLARIFY,
                product=product,
                candidate_variants=tuple(matching_variants),
                missing_options=tuple(sorted(distinct_opts)),
                reason=f"Variant options are incomplete; please choose {sorted(distinct_opts)} (T-03, S-04)",
            )

        # Exactly one variant resolved
        variant = matching_variants[0]

        # 4. Inventory, stock shortage, and unknown stock handling (T-07)
        if not variant.available_for_sale:
            return ResolutionResult(
                status=ResolutionStatus.REJECT,
                product=product,
                variant=variant,
                reason=f"Variant '{variant.variant_title}' is sold out (T-07)",
            )

        if (
            variant.quantity_available is not None
            and variant.quantity_available < target.quantity
            and variant.inventory_policy == "DENY"
        ):
            return ResolutionResult(
                status=ResolutionStatus.REJECT,
                product=product,
                variant=variant,
                reason=(
                    f"Inventory shortage: requested quantity {target.quantity} "
                    f"exceeds available stock of {variant.quantity_available} (T-07)"
                ),
            )

        stock_known = variant.quantity_available is not None

        return ResolutionResult(
            status=ResolutionStatus.RESOLVED,
            product=product,
            variant=variant,
            stock_known=stock_known,
            reason="Exact variant successfully resolved against verified evidence",
        )
