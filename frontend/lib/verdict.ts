// Centralized pre-submission verdict resolution.
//
// Driven by the probe data Carelon's clinical SPA returns at the end of
// first pass. See the corresponding plan for the reasoning; summary:
//
//   algorithm_recommendation = 3 → Approve         (93% APPROVED in our data)
//   algorithm_recommendation = 6 → In Progress     (70% APPROVED, 30% PENDED — truly uncertain)
//   gold_card_level >= 2         → Approve (bypass) (~100% APPROVED regardless of algo_rec)
//   approval_type === "no_auth"  → informational   (portal said no pre-auth needed)
//
// The old "Pend" red label is deliberately retired for algo_rec=6 because
// the historical accuracy doesn't justify a red warning.
//

export type VerdictTone = "approve" | "uncertain" | "warn" | "info" | "none";

export type Verdict = {
  label: string;        // full label, e.g. "In Progress — Outcome Uncertain"
  short: string;        // compact version for queue rows, e.g. "In Progress"
  tone: VerdictTone;
  icon: string;         // single char icon — ✓ / ⚠ / ⟳ / ⓘ
  // Tailwind classes for the three common contexts we render in.
  banner: string;       // big banner (bg + border + text)
  chip: string;         // small pill (bg + text)
  textColor: string;    // body text color when rendered inline
};

export type VerdictInput = {
  approval_type?: string | null;
  auto_approved?: boolean | null;
  gold_card_level?: number | null;
  algorithm_recommendation?: number | null;
};

// Precomputed style bundles for each tone
const STYLES: Record<VerdictTone, { banner: string; chip: string; textColor: string; icon: string }> = {
  approve: {
    banner: "bg-green-50 border border-green-300 text-green-800",
    chip: "bg-green-100 text-green-700",
    textColor: "text-green-700",
    icon: "✓",
  },
  uncertain: {
    banner: "bg-amber-50 border border-amber-300 text-amber-800",
    chip: "bg-amber-100 text-amber-700",
    textColor: "text-amber-700",
    icon: "⟳",
  },
  warn: {
    banner: "bg-red-50 border border-red-300 text-red-800",
    chip: "bg-red-100 text-red-700",
    textColor: "text-red-700",
    icon: "⚠",
  },
  info: {
    banner: "bg-sky-50 border border-sky-300 text-sky-800",
    chip: "bg-sky-100 text-sky-700",
    textColor: "text-sky-700",
    icon: "ⓘ",
  },
  none: {
    banner: "bg-gray-50 border border-gray-200 text-gray-600",
    chip: "bg-gray-100 text-gray-600",
    textColor: "text-gray-500",
    icon: "—",
  },
};

function build(tone: VerdictTone, label: string, short: string): Verdict {
  const s = STYLES[tone];
  return {
    label,
    short,
    tone,
    icon: s.icon,
    banner: s.banner,
    chip: s.chip,
    textColor: s.textColor,
  };
}

/**
 * Resolve a verdict for a case. Returns null if we have no probe data at all.
 * Priority order (top wins):
 *   1. no_auth
 *   2. Gold Card level 2+   (algorithm recommendation doesn't matter — bypass)
 *   3. Algorithm Approve    (algo_rec=3 or legacy auto_approved=true)
 *   4. In Progress          (algo_rec=6 — uncertain; replaces old "Pend")
 *   5. Likely Pend/Deny     (algo_rec is some other deny-ish value)
 *   6. Legacy Pend          (auto_approved=false, no algo_rec)
 */
export function getVerdict(c: VerdictInput): Verdict | null {
  const {
    approval_type,
    auto_approved,
    gold_card_level,
    algorithm_recommendation,
  } = c;

  // 1. No auth required
  if (approval_type === "no_auth") {
    return build("info", "No Auth Required", "No Auth");
  }

  // 2. Gold card overrides algorithm — very reliable approve signal
  if ((gold_card_level ?? 0) >= 2) {
    return build("approve", "Gold Card — Likely Approve", "Gold Card");
  }

  // 3. Algorithm approved (new primary signal: algo_rec=3)
  if (
    algorithm_recommendation === 3 ||
    (auto_approved === true && approval_type === "algorithm")
  ) {
    return build("approve", "Algorithm Approve", "Approve");
  }

  // 4. In Progress — Carelon's own label for rec=6; 70% approve, 30% pend
  if (algorithm_recommendation === 6) {
    return build("uncertain", "In Progress — Outcome Uncertain", "In Progress");
  }

  // 5. Algorithm signaled a deny-ish value (not 3, not 6, not null)
  if (
    algorithm_recommendation !== null &&
    algorithm_recommendation !== undefined
  ) {
    return build("warn", "Likely Pend/Deny", "Likely Pend");
  }

  // 6. Fallback for legacy rows that only have auto_approved
  if (auto_approved === false) {
    return build("uncertain", "Pend", "Pend");
  }

  // 7. No probe data
  return null;
}
