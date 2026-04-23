"""Parse Mongo `CPTDesc` strings into Carelon portal side/part codes.

Ingest populates `Case.body_side_desc` / `Case.body_part_desc` from this, and
the compiler uses the codes as the `preferred` arg to
`ClinicalExamFlow._parse_body_side` / `_parse_body_part` before `AddExam`.

Example inputs we see in production (confirmed in DB via raw_data.CPTDesc):
  "MRI Knee WO - LEFT"
  "MRI Knee WO - RIGHT"
  "MRI Hip WO - RIGHT"
  "MRI Shoulder W - LEFT"
  "MRI Brain WO"           (no side — body/non-paired region)
  "CT Abdomen Pelvis W"    (no side — non-paired region)

Side codes follow Carelon's code-table keys observed in HARs:
  LFT → "Left", RGT → "Right", BIL → "Bilateral".

Body-part codes follow Carelon's observed conventions (SHLDR for shoulder, etc).
The actual padded/cased strings (e.g. "LFT  ", "KNEE ") come back from
GetCptCodeBodySidePart at submit time — this parser only produces the best-effort
`preferred` token; `_parse_body_side` / `_parse_body_part` match case-insensitively
against the portal's response, so a miss degrades to empty (no regression).
"""
from __future__ import annotations

import re

# Phrase-level side matches (checked against the raw tail before tokenisation)
# so composite tokens like "B/L" are treated atomically and don't lose to a
# bare "L"/"R" after the splitter breaks them apart.
_SIDE_PHRASE_MAP: tuple[tuple[str, tuple[str, str]], ...] = (
    ("B/L", ("BIL", "Bilateral")),
)

# Token-level side matches (checked against the tokenised tail). Full words
# only — no single-letter fallbacks, which caused false positives when a
# regex split a multi-char token like "B/L" into bare "B"/"L".
_SIDE_TOKEN_MAP: tuple[tuple[str, tuple[str, str]], ...] = (
    ("BILATERAL", ("BIL", "Bilateral")),
    ("BILAT",     ("BIL", "Bilateral")),
    ("BIL",       ("BIL", "Bilateral")),
    ("LEFT",      ("LFT", "Left")),
    ("LT",        ("LFT", "Left")),
    ("RIGHT",     ("RGT", "Right")),
    ("RT",        ("RGT", "Right")),
)

# Body-part tokens. First hit in the preprocessed token list wins.
# Keys are the token we match against (uppercase); value is (portal_code, description).
_PART_MAP: tuple[tuple[str, tuple[str, str]], ...] = (
    ("SHOULDER", ("SHLDR", "Shoulder")),
    ("KNEE",     ("KNEE",  "Knee")),
    ("HIP",      ("HIP",   "Hip")),
    ("ANKLE",    ("ANKLE", "Ankle")),
    ("ELBOW",    ("ELBOW", "Elbow")),
    ("WRIST",    ("WRIST", "Wrist")),
    ("HAND",     ("HAND",  "Hand")),
    ("FOOT",     ("FOOT",  "Foot")),
    ("THIGH",    ("FEMUR", "Femur")),
    ("FEMUR",    ("FEMUR", "Femur")),
)

# Tokens we skip when hunting for the body part — these are modality /
# contrast / filler words that may appear before the anatomy.
_MODALITY_OR_CONTRAST_TOKENS: frozenset[str] = frozenset({
    "MRI", "MRA", "CT", "CTA", "XR", "XRAY", "X-RAY", "US", "ULTRASOUND",
    "PET", "PET/CT", "NM", "MAMMOGRAM", "MAMMO", "DEXA",
    "W", "WO", "W/WO", "W&WO", "W/O", "W/", "WITH", "WITHOUT", "AND",
})

# Common punctuation / separators we strip during tokenisation.
_TOKEN_SPLIT = re.compile(r"[\s/\-,.;:()\[\]]+")


def parse_cpt_desc(cpt_desc: str | None) -> tuple[str, str, str, str]:
    """Parse a Mongo `CPTDesc` string into (side_code, side_desc, part_code, part_desc).

    Returns all-empty tuple on failure or missing input. Callers use the empty
    strings as "no preference" — identical to pre-fix behavior, so there is no
    regression risk.
    """
    if not cpt_desc:
        return ("", "", "", "")

    raw = cpt_desc.strip()
    if not raw:
        return ("", "", "", "")
    upper = raw.upper()

    # --- Side: look at the suffix after the final '-', if any. ---
    side_code = ""
    side_desc = ""
    head = upper
    if "-" in upper:
        head, tail = upper.rsplit("-", 1)
        tail_stripped = tail.strip()
        matched_side = False
        # 1. Phrase-level match first (handles composite tokens like "B/L").
        for phrase, (code, desc) in _SIDE_PHRASE_MAP:
            if phrase in tail_stripped:
                side_code, side_desc = code, desc
                matched_side = True
                break
        # 2. Token-level match (full words only).
        if not matched_side:
            suffix_tokens = [t for t in _TOKEN_SPLIT.split(tail_stripped) if t]
            for key, (code, desc) in _SIDE_TOKEN_MAP:
                if key in suffix_tokens:
                    side_code, side_desc = code, desc
                    matched_side = True
                    break
        # If we didn't match a side, treat the suffix as part of the head
        # so body-part parsing doesn't miss a trailing anatomy word.
        if not matched_side:
            head = upper

    # --- Body part: scan the remaining tokens (head), skipping modality/contrast. ---
    part_code = ""
    part_desc = ""
    head_tokens = [t for t in _TOKEN_SPLIT.split(head) if t]
    body_tokens = [t for t in head_tokens if t not in _MODALITY_OR_CONTRAST_TOKENS]
    for tok in body_tokens:
        for key, (code, desc) in _PART_MAP:
            if tok == key:
                part_code, part_desc = code, desc
                break
        if part_code:
            break

    return (side_code, side_desc, part_code, part_desc)
