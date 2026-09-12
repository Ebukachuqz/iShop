"""iShop commerce engine: catalog resolution, cart authority, and reconciliation."""

from ishop.commerce.catalog import (
    CatalogResolver,
    EvidenceSnapshot,
    IntentTarget,
    ProductEvidence,
    ResolutionResult,
    ResolutionStatus,
    VariantEvidence,
)
from ishop.commerce.reconciler import (
    CommandReconciler,
    ExecutionOutcome,
    ExecutionReceipt,
)
from ishop.commerce.simulator import (
    FaultMode,
    ShopifySimulator,
    SimulatedLine,
    SimulationNetworkError,
    SimulationResult,
)
from ishop.commerce.verifier import (
    CartVerifier,
    ProposedCartAction,
    QuantityOperation,
    VerificationOutcome,
)

__all__ = [
    "CartVerifier",
    "CatalogResolver",
    "CommandReconciler",
    "EvidenceSnapshot",
    "ExecutionOutcome",
    "ExecutionReceipt",
    "FaultMode",
    "IntentTarget",
    "ProductEvidence",
    "ProposedCartAction",
    "QuantityOperation",
    "ResolutionResult",
    "ResolutionStatus",
    "ShopifySimulator",
    "SimulatedLine",
    "SimulationNetworkError",
    "SimulationResult",
    "VariantEvidence",
    "VerificationOutcome",
]
