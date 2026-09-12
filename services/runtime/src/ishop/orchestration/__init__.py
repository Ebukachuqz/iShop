"""iShop orchestration and shopping controller package.

Wires accepted transcripts, LLM interpretation, catalog resolution,
cart verification, and truthful execution receipts together.
"""

from ishop.orchestration.controller import ControllerTurnResult, ShoppingController

__all__ = [
    "ControllerTurnResult",
    "ShoppingController",
]
