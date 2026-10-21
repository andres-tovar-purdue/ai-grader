"""
Words that reveal how grading was done and must not reach students.

Terms are matched as phrases, not single words, so assignments that are
about AI agents or statistical confidence can still be discussed in
feedback ("your agent loop", "a 95% confidence interval"). Only AI
Grader's own internal vocabulary is refused.
"""

FORBIDDEN_STUDENT_LANGUAGE = (
    "agent_score",
    "agent score",
    "grading agent",
    "ai grader",
    "run_v",
    "grading confidence",
    "confidence score",
    "confidence_",
    "evidence_state",
    "evidence-state",
    "privacy flag",
    "instructor review",
    "internal validation",
    "anonymized",
)


def internal_terms(text):
    """The forbidden terms found in text (case-insensitive)."""
    lowered = (text or "").casefold()
    return [term for term in FORBIDDEN_STUDENT_LANGUAGE if term in lowered]
