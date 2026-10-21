"""
Words that reveal how grading was done and must not reach students.

Terms are matched as phrases, not single words, so assignments that are
about AI agents or statistical confidence can still be discussed in
feedback ("your agent loop", "a 95% confidence interval"). Only AI
Grader's own internal vocabulary is refused.
"""

import re

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


# Notes the grader writes for the instructor ("needs instructor review",
# "requires manual review") are moved out of student feedback into a flag.
INSTRUCTOR_NOTE_PHRASES = FORBIDDEN_STUDENT_LANGUAGE + (
    "manual review",
    "requires review",
    "needs review",
    "need review",
    "flagged for review",
    "for the instructor",
)
_SENTENCE = re.compile(r"(?<=[.!?])\s+")
_CONNECTOR = re.compile(r"(;\s+|,\s+so\s+|,\s+but\s+|,\s+and\s+|,\s+which\s+)")


def _note_in(text):
    lowered = text.casefold()
    return any(phrase in lowered for phrase in INSTRUCTOR_NOTE_PHRASES)


def remove_instructor_notes(text):
    """
    Remove the parts of student-facing text that are notes for the
    instructor. Within a sentence, a clause joined by "; ", ", so", ", but",
    ", and" or ", which" is removed on its own; a note in the main clause
    removes the whole sentence. Returns (cleaned text, removed notes).
    """
    if not text or not _note_in(text):
        return text, []
    kept, removed = [], []
    for sentence in _SENTENCE.split(text.strip()):
        if not _note_in(sentence):
            kept.append(sentence)
            continue
        ending = sentence[-1] if sentence[-1:] in ".!?" else "."
        body = sentence[:-1] if sentence[-1:] in ".!?" else sentence
        parts = _CONNECTOR.split(body)
        if _note_in(parts[0]):
            removed.append(sentence.strip())
            continue
        rebuilt = parts[0]
        for separator, part in zip(parts[1::2], parts[2::2]):
            if _note_in(part):
                removed.append(part.strip().rstrip(".!?") + ".")
            else:
                rebuilt += separator + part
        kept.append(rebuilt.rstrip(" ,;") + ending)
    return " ".join(kept).strip(), removed
