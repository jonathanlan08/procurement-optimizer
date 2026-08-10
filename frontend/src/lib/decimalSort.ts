/** Ordering for decimal wire-strings without ever going through
 * Number()/parseFloat (see lib/money.ts's file-level rule: decimal/money
 * strings can silently lose precision that way). `BigInt` parsing of a
 * digit-only string is exact/arbitrary-precision, unlike `Number()`, so
 * scaling every value to a common number of fractional digits and comparing
 * as `BigInt` preserves correct ordering for values of any size. Used to let
 * DataTable sort decimal columns (MOQ, target price) client-side. */

function toOrderableBigInt(raw: string, scale: number): bigint {
  const negative = raw.startsWith("-");
  const body = negative ? raw.slice(1) : raw;
  const [intPart = "0", fracPart = ""] = body.split(".");
  const paddedFrac = fracPart.padEnd(scale, "0").slice(0, scale);
  const digits = `${intPart}${paddedFrac}`.replace(/^0+(?=\d)/, "");
  const value = BigInt(digits === "" ? "0" : digits);
  return negative ? -value : value;
}

/** Compares two nullable decimal wire-strings; nulls sort before values. */
export function compareDecimalStrings(a: string | null, b: string | null, scale = 12): number {
  if (a === null && b === null) return 0;
  if (a === null) return -1;
  if (b === null) return 1;
  const av = toOrderableBigInt(a, scale);
  const bv = toOrderableBigInt(b, scale);
  if (av < bv) return -1;
  if (av > bv) return 1;
  return 0;
}
