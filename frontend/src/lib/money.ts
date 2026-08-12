/** Money rendering — PRINCIPAL-OWNED.
 *
 * Monetary values arrive from the API as decimal STRINGS and must never pass
 * through Number()/parseFloat, which silently corrupt precision. Formatting is
 * done by string manipulation only.
 */

const SYMBOLS: Record<string, string> = {
  USD: "$",
  EUR: "€",
  GBP: "£",
  JPY: "¥",
  CNY: "¥",
};

export interface FormatMoneyOptions {
  /** display decimal places (default 2); the full-precision value stays in tooltips */
  places?: number;
  currency?: string;
  /** show explicit + for positive values (deltas) */
  signed?: boolean;
}

/** Widest adaptive precision `formatMoney` will reach for sub-unit values —
 * matches the largest stored price scale (unit prices are NUMERIC(20,8)). */
const MAX_ADAPTIVE_PLACES = 8;

/**
 * Sub-unit values rendered at a fixed 2dp mislead: a 0.004 USD part shows as
 * $0.00 and a 0.006 one as $0.01 (a 67% distortion) — in procurement, unit
 * prices below one currency unit are routine. When |value| < 1, extend the
 * display precision just far enough to include the first TWO significant
 * fraction digits (never below `places`, capped at the stored scale), so
 * 0.004 -> "0.0040", 0.018 -> "0.018", 0.92 -> "0.92". Values >= 1 keep the
 * caller's `places` unchanged.
 */
function adaptivePlaces(raw: string, places: number): number {
  const unsigned = raw.startsWith("-") ? raw.slice(1) : raw;
  const [intPart = "0", frac = ""] = unsigned.split(".") as [string, string?];
  if (/[1-9]/.test(intPart)) return places;
  const firstSig = (frac ?? "").search(/[1-9]/);
  if (firstSig === -1) return places; // true zero
  return Math.min(Math.max(places, firstSig + 2), MAX_ADAPTIVE_PLACES);
}

/** Validate a decimal string: optional sign, digits, optional fraction. */
export function isDecimalString(raw: string): boolean {
  return /^-?\d+(\.\d+)?$/.test(raw);
}

/**
 * Round a decimal string to `places` using half-even (matches the backend's
 * banker's rounding), returning a decimal string. Pure string/bigint math.
 */
export function roundDecimalString(raw: string, places: number): string {
  if (!isDecimalString(raw)) throw new Error(`not a decimal string: ${raw}`);
  const negative = raw.startsWith("-");
  const [intRaw, fracRaw = ""] = (negative ? raw.slice(1) : raw).split(".") as [
    string,
    string?,
  ];
  const frac = fracRaw ?? "";
  if (frac.length <= places) {
    const padded = frac.padEnd(places, "0");
    const body = places > 0 ? `${intRaw}.${padded}` : intRaw;
    return negative && !/^0(\.0*)?$/.test(body) ? `-${body}` : body;
  }
  // scale to an integer of `places` digits, then apply half-even on the rest
  const keep = BigInt(intRaw + frac.slice(0, places));
  const restDigits = frac.slice(places);
  const firstRest = restDigits[0] ?? "0";
  let rounded = keep;
  if (firstRest > "5") rounded = keep + 1n;
  else if (firstRest === "5") {
    const restNonZero = /[1-9]/.test(restDigits.slice(1));
    if (restNonZero || keep % 2n === 1n) rounded = keep + 1n;
  }
  let digits = rounded.toString().padStart(places + 1, "0");
  const intPart = digits.slice(0, digits.length - places) || "0";
  const fracPart = places > 0 ? digits.slice(digits.length - places) : "";
  const body = places > 0 ? `${intPart}.${fracPart}` : intPart;
  return negative && !/^0(\.0*)?$/.test(body) ? `-${body}` : body;
}

/** Group integer digits with thousands separators, string-only. */
function groupThousands(intPart: string): string {
  return intPart.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

export function formatMoney(raw: string, opts: FormatMoneyOptions = {}): string {
  const { places = 2, currency, signed = false } = opts;
  const rounded = roundDecimalString(raw, adaptivePlaces(raw, places));
  const negative = rounded.startsWith("-");
  const [intPart = "0", fracPart = ""] = (negative ? rounded.slice(1) : rounded).split(
    ".",
  ) as [string, string?];
  const symbol = currency ? (SYMBOLS[currency] ?? `${currency} `) : "";
  const grouped = groupThousands(intPart);
  const body = fracPart ? `${grouped}.${fracPart}` : grouped;
  const sign = negative ? "−" : signed ? "+" : "";
  return `${sign}${symbol}${body}`;
}

export interface FormatDecimalOptions {
  /** minimum decimal places kept after trimming (default 2) */
  minPlaces?: number;
}

/**
 * Display a plain decimal string (FX rates, quantities, assumption values) at
 * its stored precision with the noise removed: trailing fraction zeros are
 * trimmed, but never below `minPlaces` — "0.920000000000" -> "0.92",
 * "199.500000000000" -> "199.50", "0.004" -> "0.004". String-only; the input
 * is returned untouched if it is not a decimal string.
 */
export function formatDecimal(raw: string, opts: FormatDecimalOptions = {}): string {
  const { minPlaces = 2 } = opts;
  if (!isDecimalString(raw)) return raw;
  const negative = raw.startsWith("-");
  const [intPart = "0", frac = ""] = (negative ? raw.slice(1) : raw).split(".") as [
    string,
    string?,
  ];
  let kept = (frac ?? "").replace(/0+$/, "");
  if (kept.length < minPlaces) kept = (frac ?? "").slice(0, minPlaces).padEnd(minPlaces, "0");
  const body = kept ? `${intPart}.${kept}` : intPart;
  return negative && !/^0(\.0*)?$/.test(body) ? `-${body}` : body;
}
