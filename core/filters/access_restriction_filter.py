"""
core/filters/access_restriction_filter.py
──────────────────────
Production-grade detector for security-clearance, citizenship, and
export-control *requirements* inside job descriptions.

Design decisions
────────────────
1. Sentence-level matching   – patterns are tested sentence-by-sentence so
                               a `.*` can never jump across bullet points or
                               unrelated clauses.
2. Restricted wildcards      – `.*` is replaced with `[^\n.!?]{0,120}` to
                               prevent runaway cross-sentence matches even
                               inside a single sentence.
3. Negation guards           – each sentence that matches a positive pattern
                               is also checked against a negation list before
                               being counted.
4. Bullet / header phrases   – bare phrases that appear as standalone bullet
                               items ("• U.S. Citizen") are covered by a
                               dedicated "standalone" pattern list.
5. Compile once              – all patterns are compiled once at import time
                               to avoid per-call overhead.
6. Debug logging             – match details (sentence + pattern) are logged
                               at DEBUG level for easy audit.
"""

import re
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

# Max characters a wildcard span may cover inside one sentence (not cross-sentence).
_W = r"[^\n.!?]{0,120}"

_FLAGS = re.IGNORECASE


def _compile_all(patterns: list[str]) -> list[re.Pattern]:
    return [re.compile(p, _FLAGS) for p in patterns]


def _split_sentences(text: str) -> list[str]:
    """
    Split text into coarse sentences / logical units while preserving
    common abbreviations (U.S., e.g., i.e., TS/SCI, DoD, etc.) so that
    the dot inside them does not cause a spurious split.

    Strategy
    ────────
    1. Temporarily replace known abbreviation dots with a placeholder.
    2. Split on sentence-ending punctuation (. ! ?), newlines, and
       bullet / list markers.
    3. Restore placeholders before returning.
    """
    # ── Step 1: protect abbreviation dots ──────────────────────────────────
    _PLACEHOLDER = "\x00"   # NUL – never appears in real job descriptions

    # Ordered from most-specific to least-specific to avoid partial clobbers.
    _ABBREV_PATTERNS = [
        # U.S. / U.S.A. / u.s. – with optional trailing space or word char
        (re.compile(r"\bU\.S\.A?\.?", re.IGNORECASE), lambda m: m.group().replace(".", _PLACEHOLDER)),
        # Common two-letter + dot abbreviations: e.g., i.e., etc., vs., Mr., Dr.
        (re.compile(r"\b(?:e\.g|i\.e|etc|vs|mr|dr|sr|jr)\.", re.IGNORECASE), lambda m: m.group().replace(".", _PLACEHOLDER)),
        # DoD, TS/SCI, EAR, ITAR – all-caps acronyms (no dots to protect, kept for completeness)
    ]

    protected = text
    for pat, repl in _ABBREV_PATTERNS:
        protected = pat.sub(repl, protected)

    # ── Step 2: split on sentence boundaries and list markers ──────────────
    # Sentence endings: only split on '.' that is NOT inside parentheses and
    # is followed by whitespace or end-of-string (guards "compliance (EAR).")
    # Also split on: newline, carriage return, !, ?, ;
    # List markers: lines beginning with -, –, •, *, or a digit+dot (1. 2.)
    parts = re.split(
        r"""
        (?<=[^A-Z\d])           # negative lookbehind: not after UPPER or digit
        \.                      # literal dot (sentence-ending)
        (?=\s|$)                # followed by whitespace or end
        |
        [!\?;]                  # other sentence terminators
        |
        [\n\r]+                 # newlines
        |
        (?:^|(?<=\n))\s*        # start of line
        (?:[-–•*]|\d+[\.\)])\s+ # bullet or numbered list marker
        """,
        protected,
        flags=re.VERBOSE | re.MULTILINE,
    )

    # ── Step 3: restore placeholders ──────────────────────────────────────
    restored = [p.replace(_PLACEHOLDER, ".").strip() for p in parts]
    return [s for s in restored if s]


def _sentence_matches(
    sentence: str,
    positive_patterns: list[re.Pattern],
    negation_patterns: list[re.Pattern],
    label: str,
) -> bool:
    """
    Return True if *sentence* matches any positive pattern AND is not
    cancelled by any negation pattern.
    """
    for pat in positive_patterns:
        if pat.search(sentence):
            # Check negation inside the same sentence
            for neg in negation_patterns:
                if neg.search(sentence):
                    logger.debug(
                        "[%s] Positive match negated  | sentence=%r | neg=%s",
                        label, sentence, neg.pattern,
                    )
                    return False
            logger.debug(
                "[%s] Positive match confirmed | sentence=%r | pattern=%s",
                label, sentence, pat.pattern,
            )
            return True
    return False


# ──────────────────────────────────────────────────────────────────────────────
# NEGATION patterns  (shared across all three detectors)
# ──────────────────────────────────────────────────────────────────────────────

_NEGATION_RAW = [
    # Direct negations
    r"not\s+required",
    r"not\s+needed",
    r"not\s+mandatory",
    r"no\s+(?:security\s+)?clearance\s+(?:is\s+)?required",
    r"without\s+(?:a\s+)?clearance",
    r"clearance\s+is\s+not",
    r"citizenship\s+is\s+not",
    r"does\s+not\s+require",
    r"we\s+do\s+not\s+require",
    r"not\s+a\s+requirement",
    r"no\s+(?:citizenship|clearance|export\s+control)\s+requirement",
    # Preference / nice-to-have signals (NOT requirements)
    r"\b(?:preferred|desirable|a\s+plus|bonus|nice[\s-]to[\s-]have|advantageous)\b",
    # Future / optional framing
    r"may\s+be\s+required\s+(?:in\s+the\s+future|at\s+a\s+later)",
    r"could\s+be\s+required\s+later",
    r"potential\s+future\s+requirement",
    # Benefits / company perks mention (e.g. "we sponsor clearances")
    # Use word boundary so "without sponsorship" is NOT caught here
    r"\bwe\s+(?:will\s+)?sponsor\b",
    r"\bvisa\s+sponsorship\s+(?:is\s+)?(?:available|provided|offered)\b",
    r"can\s+obtain\s+upon\s+hire",
]

_NEGATIONS: list[re.Pattern] = _compile_all(_NEGATION_RAW)


# ──────────────────────────────────────────────────────────────────────────────
# SECURITY CLEARANCE patterns
# ──────────────────────────────────────────────────────────────────────────────

_CLEARANCE_POSITIVE_RAW = [
    # ── Explicit requirement verbs ──
    r"must\s+(?:obtain|hold|have|possess|maintain)" + _W + r"clearance",
    r"required?\s+to\s+(?:obtain|hold|have|possess|maintain)" + _W + r"clearance",
    r"clearance\s+(?:is\s+)?(?:required|needed|mandatory|essential|necessary)",
    r"active\s+(?:and\s+)?(?:valid\s+)?(?:security\s+)?clearance\s+(?:required|needed|mandatory|preferred\s+not\s+applicable)",
    r"(?:current|valid|active)\s+(?:security\s+)?clearance",

    # ── Classification levels ──
    r"\b(?:TS|SCI|TS/SCI|TS\s*[-/]\s*SCI|top\s+secret(?:/SCI)?)\b" + _W + r"(?:required|needed|mandatory|must|clearance)",
    r"\b(?:secret|confidential)\s+clearance\s+(?:required|needed|mandatory)",
    r"public\s+trust\s+(?:clearance\s+)?(?:required|needed|mandatory)",
    r"DoD\s+(?:security\s+)?clearance\s+(?:required|needed|mandatory)",
    r"(?:SCI|SAP|SAR|SSBI|Poly|polygraph)\s+(?:clearance\s+)?(?:required|eligible|needed)",

    # ── Ability / eligibility framing ──
    r"(?:ability|eligible|eligibility)\s+to\s+obtain\s+(?:a\s+)?(?:security\s+)?clearance",
    r"capable\s+of\s+obtaining\s+(?:a\s+)?(?:security\s+)?clearance",
    r"clearance\s+eligible",
    r"must\s+be\s+clearable",

    # ── Standalone bullet phrases (dash is consumed by splitter) ──
    r"^(?:active|current|valid)\s+(?:security\s+)?clearance$",
    r"^(?:TS|SCI|TS/SCI|Top\s+Secret)(?:\s+clearance)?$",
    r"^(?:requires?|require[sd]?)\s+clearance$",
    r"^clearance\s+required$",
    r"^must\s+hold\s+clearance$",
    # Also match when the full bullet text is just the level name (after strip)
    r"^\s*(?:TS|SCI|TS/SCI|Top\s+Secret|Secret|Confidential)(?:/SCI)?\s*$",
]

_CLEARANCE_POSITIVE: list[re.Pattern] = _compile_all(_CLEARANCE_POSITIVE_RAW)

# Clearance-specific negations (supplement the shared list)
_CLEARANCE_NEGATION_RAW = _NEGATION_RAW + [
    r"clearance\s+(?:is\s+)?(?:not|un)(?:necessary|needed|required)",
    r"no\s+clearance\s+needed",
    r"open\s+to\s+candidates\s+without\s+clearance",
]

_CLEARANCE_NEGATIONS: list[re.Pattern] = _compile_all(_CLEARANCE_NEGATION_RAW)


# ──────────────────────────────────────────────────────────────────────────────
# CITIZENSHIP patterns
# ──────────────────────────────────────────────────────────────────────────────

_CITIZENSHIP_POSITIVE_RAW = [
    # ── Must be / must have ──
    r"must\s+be\s+(?:a\s+)?u\.?s\.?\s+citizen",
    r"must\s+have\s+(?:u\.?s\.?\s+)?citizenship",
    r"required?\s+to\s+be\s+(?:a\s+)?u\.?s\.?\s+citizen",

    # ── Citizenship is required / only ──
    r"u\.?s\.?\s+citizenship\s+(?:is\s+)?(?:required|needed|mandatory|essential|necessary)",
    r"u\.?s\.?\s+citizen(?:s)?\s+only",
    r"united\s+states\s+citizen(?:ship)?\s+(?:only|required|needed|mandatory)",
    r"citizen(?:ship)?\s+(?:of\s+)?(?:the\s+)?(?:united\s+states|u\.?s\.?)\s+(?:required|needed|mandatory|only)",

    # ── Eligibility / authorization to work ──
    r"authorized\s+to\s+work\s+(?:in\s+)?(?:the\s+)?(?:u\.?s\.?|united\s+states)",
    r"eligible\s+to\s+work\s+(?:in\s+)?(?:the\s+)?(?:u\.?s\.?|united\s+states)",
    r"legal\s+(?:right|authorization)\s+to\s+work\s+in\s+(?:the\s+)?(?:u\.?s\.?|united\s+states)",
    r"(?:work\s+)?authorization\s+(?:in\s+)?(?:the\s+)?(?:u\.?s\.?)\s+(?:required|needed|mandatory)",

    # ── U.S. Person (legal term) ──
    r"must\s+be\s+(?:a\s+)?u\.?s\.?\s+person",
    r"u\.?s\.?\s+person\s+(?:only|required|needed|mandatory)",
    r"qualified\s+u\.?s\.?\s+person",

    # ── Standalone bullets ──
    r"^u\.?s\.?\s+citizen(?:ship)?(?:\s+required)?$",
    r"^(?:must\s+be\s+a?\s+)?(?:u\.?s\.?|american)\s+citizen$",
    r"^citizenship\s+required$",
    r"^authorized\s+to\s+work\s+in\s+the\s+u\.?s\.?$",
    r"^(?:u\.?s\.?\s+)?work\s+authorization\s+required$",
]

_CITIZENSHIP_POSITIVE: list[re.Pattern] = _compile_all(_CITIZENSHIP_POSITIVE_RAW)

_CITIZENSHIP_NEGATION_RAW = _NEGATION_RAW + [
    r"any\s+(?:national|citizenship|background)",
    r"open\s+to\s+all\s+(?:citizens|nationalities)",
    r"international\s+candidates?\s+welcome",
    r"visa\s+(?:sponsorship\s+)?(?:available|provided|offered)",
    r"will\s+(?:consider|accept)\s+(?:visa|OPT|CPT|H[\s-]?1B)",
    r"no\s+sponsorship\s+available",  # "no sponsorship" ≠ citizenship requirement
]

_CITIZENSHIP_NEGATIONS: list[re.Pattern] = _compile_all(_CITIZENSHIP_NEGATION_RAW)


# ──────────────────────────────────────────────────────────────────────────────
# EXPORT CONTROL patterns
# ──────────────────────────────────────────────────────────────────────────────

_EXPORT_POSITIVE_RAW = [
    # ── Must meet / comply with ──
    r"must\s+(?:meet|comply\s+with|satisfy)" + _W + r"export\s+control",
    r"export\s+control\s+(?:compliance|requirements?|restrictions?|laws?)\s+(?:required|apply|applies|mandatory|needed)",
    r"subject\s+to\s+(?:u\.?s\.?\s+)?export\s+control",

    # ── ITAR / EAR ──
    r"\bITAR\b" + _W + r"(?:required|restricted|controlled|compliance|eligible|applies|covered)",
    r"(?:required|restricted|controlled|compliance|eligible|applies|covered)" + _W + r"\bITAR\b",
    r"ITAR[\s-]controlled",
    r"\bEAR\b" + _W + r"(?:controlled|compliance|restrictions?|regulations?)",
    r"(?:ear|itar)\s+(?:regulated|controlled|compliant)",
    # EAR/ITAR mentioned parenthetically alongside "required" in the same sentence
    r"export\s+control\s+compliance\s*\([^)]*(?:EAR|ITAR)[^)]*\)\s*(?:is\s+)?(?:required|needed|mandatory|applies)",
    r"(?:EAR|ITAR)\s*\)\s*(?:is\s+)?(?:required|needed|mandatory|applies)",

    # ── Deemed export ──
    r"deemed\s+export(?:ed)?",
    r"deemed\s+export\s+(?:controlled|control|compliance|requirements?)",

    # ── U.S. Person (for export law purposes) ──
    r"u\.?s\.?\s+person(?:s)?\s+(?:only|required|as\s+defined\s+by\s+(?:ITAR|EAR|export))",
    r"must\s+qualify\s+as\s+a\s+u\.?s\.?\s+person",
    r"(?:ITAR|EAR)\s+definition\s+of\s+(?:a\s+)?u\.?s\.?\s+person",

    # ── Standalone bullets ──
    r"^ITAR\s+(?:controlled|compliance|restricted|eligible)?$",
    r"^export\s+control(?:led)?\s+(?:position|role|compliance)?$",
    r"^must\s+be\s+(?:a\s+)?u\.?s\.?\s+person$",
]

_EXPORT_POSITIVE: list[re.Pattern] = _compile_all(_EXPORT_POSITIVE_RAW)

_EXPORT_NEGATION_RAW = _NEGATION_RAW + [
    r"experience\s+with\s+ITAR",           # Familiarity ≠ restriction
    r"familiar(?:ity)?\s+with\s+(?:ITAR|EAR|export\s+control)",
    r"knowledge\s+of\s+(?:ITAR|EAR|export\s+control)",
    r"background\s+in\s+(?:ITAR|EAR|export\s+control)",
    r"ITAR\s+(?:environment|awareness|training)",   # awareness ≠ restriction
    r"export\s+control\s+(?:training|awareness|experience)",
]

_EXPORT_NEGATIONS: list[re.Pattern] = _compile_all(_EXPORT_NEGATION_RAW)


# ──────────────────────────────────────────────────────────────────────────────
# Public detection functions
# ──────────────────────────────────────────────────────────────────────────────

def has_clearance_requirement(description: str) -> bool:
    """
    Return True if the job description explicitly requires a security clearance.

    Checks sentence-by-sentence and applies negation guards so that
    "clearance is not required" or "experience with clearance environments
    is a plus" do not trigger a positive result.
    """
    if not description:
        return False

    for sentence in _split_sentences(description):
        if _sentence_matches(
            sentence,
            _CLEARANCE_POSITIVE,
            _CLEARANCE_NEGATIONS,
            label="CLEARANCE",
        ):
            return True
    return False


def has_citizenship_requirement(description: str) -> bool:
    """
    Return True if the job description explicitly requires U.S. citizenship
    or equivalent work-authorization status.

    Distinguishes genuine requirements from informational mentions or
    sponsorship-related statements.
    """
    if not description:
        return False

    for sentence in _split_sentences(description):
        if _sentence_matches(
            sentence,
            _CITIZENSHIP_POSITIVE,
            _CITIZENSHIP_NEGATIONS,
            label="CITIZENSHIP",
        ):
            return True
    return False


def has_export_control_requirement(description: str) -> bool:
    """
    Return True if the job description applies export-control restrictions
    (ITAR, EAR, deemed-export, U.S. Person requirements).

    Distinguishes "must comply with ITAR" from "experience with ITAR
    environments is a plus."
    """
    if not description:
        return False

    for sentence in _split_sentences(description):
        if _sentence_matches(
            sentence,
            _EXPORT_POSITIVE,
            _EXPORT_NEGATIONS,
            label="EXPORT_CONTROL",
        ):
            return True
    return False


def has_access_restrictions(job) -> bool:
    """
    Aggregate check: returns True if the job has *any* clearance, citizenship,
    or export-control requirement.
    """
    if not getattr(job, "description", None):
        return False

    desc = job.description
    return (
        has_clearance_requirement(desc)
        or has_citizenship_requirement(desc)
        or has_export_control_requirement(desc)
    )


# ──────────────────────────────────────────────────────────────────────────────
# Quick smoke-test  (run: python clearance_detection.py)
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)

    cases = [
        # ── Should be TRUE ──────────────────────────────────────────────────
        ("CLEARANCE TRUE",
         "Candidates must obtain a Top Secret clearance before starting."),
        ("CLEARANCE TRUE – active",
         "An active TS/SCI clearance is required for this position."),
        ("CLEARANCE TRUE – eligible",
         "Ability to obtain a DoD Secret clearance is required."),
        ("CLEARANCE TRUE – bullet",
         "Requirements:\n- TS/SCI\n- 5+ years of experience"),
        ("CITIZENSHIP TRUE",
         "Must be a U.S. Citizen. Green card holders are not eligible."),
        ("CITIZENSHIP TRUE – authorization",
         "Candidates must be authorized to work in the U.S. without sponsorship."),
        ("CITIZENSHIP TRUE – U.S. person",
         "This role requires a qualified U.S. person as defined by ITAR."),
        ("EXPORT TRUE – ITAR controlled",
         "This position is ITAR-controlled; applicants must be U.S. persons."),
        ("EXPORT TRUE – deemed export",
         "Due to deemed export requirements, only U.S. persons may apply."),
        ("EXPORT TRUE – EAR",
         "Export control compliance (EAR) is required for this role."),

        # ── Should be FALSE ─────────────────────────────────────────────────
        ("CLEARANCE FALSE – negated",
         "Security clearance is not required for this role."),
        ("CLEARANCE FALSE – future maybe",
         "A clearance may be required in the future depending on project needs."),
        ("CLEARANCE FALSE – nice-to-have",
         "Active clearance is preferred but not required."),
        ("CITIZENSHIP FALSE – visa sponsored",
         "We offer visa sponsorship. U.S. citizenship is not required."),
        ("CITIZENSHIP FALSE – open",
         "We welcome international candidates. No citizenship requirement."),
        ("EXPORT FALSE – familiarity",
         "Experience with ITAR environments is a plus."),
        ("EXPORT FALSE – training",
         "You will receive ITAR awareness training during onboarding."),
        ("EXPORT FALSE – knowledge of EAR",
         "Knowledge of EAR export control regulations is preferred."),
    ]

    print(f"\n{'Label':<38} {'Expected':<10} {'Got':<10} {'Pass?'}")
    print("─" * 68)

    expected_results = (
        [True] * 10 +   # first 10 should be True
        [False] * 8     # last 8 should be False
    )

    funcs = {
        "CLEARANCE": has_clearance_requirement,
        "CITIZENSHIP": has_citizenship_requirement,
        "EXPORT": has_export_control_requirement,
    }

    for (label, desc), expected in zip(cases, expected_results):
        key = next((k for k in funcs if label.startswith(k)), None)
        got = funcs[key](desc) if key else False
        status = "✅" if got == expected else "❌"
        print(f"{label:<38} {str(expected):<10} {str(got):<10} {status}")