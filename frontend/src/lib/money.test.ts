import { describe, expect, it } from "vitest";
import { formatMoney, isDecimalString, roundDecimalString } from "./money";

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
});
