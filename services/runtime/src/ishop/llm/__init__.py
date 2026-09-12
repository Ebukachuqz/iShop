"""iShop LLM reasoning and provider abstraction package.

Contains provider-neutral interfaces, Gemini and Groq adapters,
offline deterministic provider, and versioned prompts.
"""

from ishop.llm.base import (
    GroundedResponseContext,
    LlmIntentRequest,
    LlmInterpretationResult,
    LlmProfile,
    LlmProvider,
    LlmProviderError,
    LlmRegistry,
    LlmUsage,
)
from ishop.llm.fake import FakeLlmProvider
from ishop.llm.gemini import GeminiLlmProvider
from ishop.llm.groq import GroqLlmProvider
from ishop.llm.prompts import (
    GROUNDED_RESPONSE_SYSTEM_PROMPT,
    SYSTEM_INTENT_PROMPT,
    format_intent_user_prompt,
)

__all__ = [
    "FakeLlmProvider",
    "GeminiLlmProvider",
    "GroqLlmProvider",
    "GroundedResponseContext",
    "GROUNDED_RESPONSE_SYSTEM_PROMPT",
    "LlmIntentRequest",
    "LlmInterpretationResult",
    "LlmProfile",
    "LlmProvider",
    "LlmProviderError",
    "LlmRegistry",
    "LlmUsage",
    "SYSTEM_INTENT_PROMPT",
    "format_intent_user_prompt",
]
