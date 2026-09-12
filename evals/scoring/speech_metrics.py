"""Speech evaluation metrics: WER and CER for iShop (Drake).

Enforces:
- T-24: Every case in denominator; empty references tracked as hallucinations rather than silent skips.
- T-25: Exact Levenshtein distance alignment with versioned normalization.
"""

from __future__ import annotations

from dataclasses import dataclass
from evals.scoring.text_norm import normalize_text


@dataclass(frozen=True)
class AlignmentResult:
    error_rate: float
    substitutions: int
    deletions: int
    insertions: int
    ref_length: int
    is_hallucination: bool = False


def compute_levenshtein(ref_tokens: list[str], hyp_tokens: list[str]) -> tuple[int, int, int]:
    """Computes substitutions, deletions, and insertions via dynamic programming."""
    n = len(ref_tokens)
    m = len(hyp_tokens)

    # dp[i][j] = (dist, subs, dels, ins)
    # To save memory, track previous and current row
    dp = [[(0, 0, 0, 0) for _ in range(m + 1)] for _ in range(n + 1)]

    for i in range(n + 1):
        dp[i][0] = (i, 0, i, 0)  # i deletions
    for j in range(m + 1):
        dp[0][j] = (j, 0, 0, j)  # j insertions

    for i in range(1, n + 1):
        for j in range(1, m + 1):
            if ref_tokens[i - 1] == hyp_tokens[j - 1]:
                dp[i][j] = dp[i - 1][j - 1]
            else:
                # Sub
                sub_cost = dp[i - 1][j - 1][0] + 1
                sub_tup = (sub_cost, dp[i - 1][j - 1][1] + 1, dp[i - 1][j - 1][2], dp[i - 1][j - 1][3])

                # Del
                del_cost = dp[i - 1][j][0] + 1
                del_tup = (del_cost, dp[i - 1][j][1], dp[i - 1][j][2] + 1, dp[i - 1][j][3])

                # Ins
                ins_cost = dp[i][j - 1][0] + 1
                ins_tup = (ins_cost, dp[i][j - 1][1], dp[i][j - 1][2], dp[i][j - 1][3] + 1)

                best = min((sub_tup, del_tup, ins_tup), key=lambda x: x[0])
                dp[i][j] = best

    _, s, d, ins = dp[n][m]
    return s, d, ins


def calculate_wer(
    ref: str,
    hyp: str,
    norm_version: str = "ishop-unicode-v1",
) -> AlignmentResult:
    """Calculates Word Error Rate (WER)."""
    norm_ref = normalize_text(ref, version=norm_version)
    norm_hyp = normalize_text(hyp, version=norm_version)

    ref_words = norm_ref.split() if norm_ref else []
    hyp_words = norm_hyp.split() if norm_hyp else []

    ref_len = len(ref_words)
    hyp_len = len(hyp_words)

    if ref_len == 0:
        if hyp_len == 0:
            return AlignmentResult(
                error_rate=0.0,
                substitutions=0,
                deletions=0,
                insertions=0,
                ref_length=0,
                is_hallucination=False,
            )
        else:
            # Hallucination on silence/empty audio (T-24)
            return AlignmentResult(
                error_rate=1.0,
                substitutions=0,
                deletions=0,
                insertions=hyp_len,
                ref_length=0,
                is_hallucination=True,
            )

    s, d, ins = compute_levenshtein(ref_words, hyp_words)
    wer = (s + d + ins) / float(ref_len)
    return AlignmentResult(
        error_rate=wer,
        substitutions=s,
        deletions=d,
        insertions=ins,
        ref_length=ref_len,
        is_hallucination=False,
    )


def calculate_cer(
    ref: str,
    hyp: str,
    norm_version: str = "ishop-unicode-v1",
) -> AlignmentResult:
    """Calculates Character Error Rate (CER)."""
    norm_ref = normalize_text(ref, version=norm_version)
    norm_hyp = normalize_text(hyp, version=norm_version)

    # Characters excluding whitespace
    ref_chars = [c for c in norm_ref if not c.isspace()]
    hyp_chars = [c for c in norm_hyp if not c.isspace()]

    ref_len = len(ref_chars)
    hyp_len = len(hyp_chars)

    if ref_len == 0:
        if hyp_len == 0:
            return AlignmentResult(0.0, 0, 0, 0, 0, is_hallucination=False)
        else:
            return AlignmentResult(1.0, 0, 0, hyp_len, 0, is_hallucination=True)

    s, d, ins = compute_levenshtein(ref_chars, hyp_chars)
    cer = (s + d + ins) / float(ref_len)
    return AlignmentResult(
        error_rate=cer,
        substitutions=s,
        deletions=d,
        insertions=ins,
        ref_length=ref_len,
        is_hallucination=False,
    )
