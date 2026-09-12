"""Catalog and variant resolution for iShop (Drake).

Enforces Safety invariants:
- S-03: Facts come from Shopify evidence; reject invented product IDs or wrong currency (T-02).
- S-04: No silent variant substitution; incomplete options require clarification (T-03).
- T-07: Sold out variants and shortages reject false in-stock promises; unknown stock stays unknown.
"""

from __future__ import annotations

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


@dataclass(frozen=True)
class EvidenceSnapshot:
    """Point-in-time catalog evidence snapshot bound to shop and currency."""

    snapshot_id: str
    shop_id: str
    currency: str
    observed_at_ms: int
    products: dict[str, ProductEvidence] = field(default_factory=dict)


@dataclass(frozen=True)
class IntentTarget:
    """Target reference requested by shopper or interpreted by LLM."""

    product_id: str | None = None
    title_query: str | None = None
    selected_options: dict[str, str] = field(default_factory=dict)
    quantity: int = 1

    def __post_init__(self):
        if self.quantity < 1:
            raise ValueError(f"Target quantity must be >= 1, got {self.quantity}")
        norm_opts = {str(k).lower(): str(v).lower() for k, v in self.selected_options.items()}
        object.__setattr__(self, "selected_options", norm_opts)


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
            query = target.title_query.strip().lower()
            matching_products = [
                p for p in evidence.products.values()
                if query in p.title.lower() or p.title.lower() in query
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
            if valid_vals_for_opt and opt_val not in valid_vals_for_opt:
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
                if variant.selected_options.get(opt_key) != opt_val:
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
