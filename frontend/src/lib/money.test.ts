import { describe, expect, it } from "vitest";
import { formatDecimal, formatMoney, isDecimalString, roundDecimalString } from "./money";

describe("isDecimalString", () => {
  it("accepts plain decimals", () => {
    expect(isDecimalString("12.50")).toBe(true);
    expect(isDecimalString("-37.522335")).toBe(true);
    expect(isDecimalString("500")).toBe(true);
  });
  it("rejects garbage and floats-in-disguise", () => {
    expect(isDecimalString("12,50")).toBe(false);
    expect(isDecimalString("1e6")).toBe(false);
    expect(isDecimalString("NaN")).toBe(false);
    expect(isDecimalString("")).toBe(false);
  });
});

describe("roundDecimalString (half-even, string-only)", () => {
  it("pads shorter fractions", () => {
    expect(roundDecimalString("12", 2)).toBe("12.00");
    expect(roundDecimalString("12.5", 2)).toBe("12.50");
  });
  it("applies banker's rounding at the boundary", () => {
    expect(roundDecimalString("1.005", 2)).toBe("1.00"); // 0 is even
    expect(roundDecimalString("1.015", 2)).toBe("1.02"); // 1 -> 2
    expect(roundDecimalString("1.0051", 2)).toBe("1.01"); // rest non-zero
  });
  it("keeps precision beyond Number range", () => {
    expect(roundDecimalString("90071992547409929.135", 2)).toBe("90071992547409929.14");
  });
  it("handles negatives", () => {
    expect(roundDecimalString("-37.522335", 2)).toBe("-37.52");
  });
  it("never returns negative zero", () => {
    expect(roundDecimalString("-0.001", 2)).toBe("0.00");
  });
});

describe("formatMoney", () => {
  it("groups thousands and applies currency", () => {
    expect(formatMoney("7240.548318", { currency: "USD" })).toBe("$7,240.55");
    expect(formatMoney("1234567.891", { currency: "EUR" })).toBe("€1,234,567.89");
  });
  it("uses a code prefix for unknown currencies", () => {
    expect(formatMoney("100", { currency: "SEK" })).toBe("SEK 100.00");
  });
  it("shows signed deltas with a real minus sign", () => {
    expect(formatMoney("-37.522335", { currency: "USD" })).toBe("−$37.52");
    expect(formatMoney("5.1", { currency: "USD", signed: true })).toBe("+$5.10");
  });
  it("supports more display places", () => {
    expect(formatMoney("14.48109664", { places: 4 })).toBe("14.4811");
  });
  it("extends precision for sub-unit values instead of misrounding them", () => {
    // review-3 P1: a 0.004 USD part rendered as $0.00, 0.006 as $0.01 (67% off)
    expect(formatMoney("0.004", { currency: "USD" })).toBe("$0.0040");
    expect(formatMoney("0.006000", { currency: "USD" })).toBe("$0.0060");
    expect(formatMoney("0.018", { currency: "USD" })).toBe("$0.018");
    expect(formatMoney("-0.004", { currency: "USD" })).toBe("−$0.0040");
    // values >= 1 and true zero keep the caller's places
    expect(formatMoney("1.004", { currency: "USD" })).toBe("$1.00");
    expect(formatMoney("0.000000", { currency: "USD" })).toBe("$0.00");
    // never exceeds the stored 8dp scale
    expect(formatMoney("0.00000001", { currency: "USD" })).toBe("$0.00000001");
  });
});

describe("formatDecimal", () => {
  it("trims stored-scale noise but keeps at least two places", () => {
    expect(formatDecimal("0.920000000000")).toBe("0.92");
    expect(formatDecimal("199.500000000000")).toBe("199.50");
    expect(formatDecimal("100")).toBe("100.00");
    expect(formatDecimal("0.004000")).toBe("0.004");
    expect(formatDecimal("-1.250000")).toBe("-1.25");
  });
  it("returns non-decimal input untouched", () => {
    expect(formatDecimal("n/a")).toBe("n/a");
  });
});
