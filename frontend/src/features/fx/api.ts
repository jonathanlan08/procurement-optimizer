/** FX data layer - TanStack Query hooks over the live API.
 *
 * Shapes mirror backend/src/app/schemas/fx.py exactly:
 *  - `rate` is a 12dp `RateString` wire string (`NUMERIC(24,12)`,
 *    `RATE_SCALE`) - its own scale, distinct from the 6dp/8dp money scales
 *    used elsewhere (lib/money.ts's `isDecimalString` still applies: never
 *    run through Number()/parseFloat).
 *  - `GET /exchange-rates` (api/v1/fx.py) serves two different shapes from
 *    one route, selected by which query params are present:
 *     1. **Effective-rate lookup** - `base` + `quote` + `as_of` ALL given:
 *        returns a single `EffectiveExchangeRateResponse` (rate + its
 *        provenance: `source` is `"synthetic_fixture"` or `"manual"`,
 *        never persisted for the fixture case - `exchange_rate_id` is
 *        `null` then). `useEffectiveRate` is disabled until all three
 *        inputs are non-blank, exactly mirroring the backend's own
 *        all-three-or-none branch (api/v1/fx.py `list_or_get_effective_
 *        exchange_rate`) rather than guessing at a partial answer.
 *     2. **Otherwise** - a plain paginated listing of this org's own
 *        stored manual-override rows (`ExchangeRateListResponse`, `{items,
 *        page}`), optionally filtered by `base`/`quote` alone. `as_of` is
 *        NOT a list filter (the route only reads it as part of the
 *        all-three effective-lookup branch), so `useExchangeRates` doesn't
 *        accept it.
 *  - `POST /exchange-rates` body fields are `base_currency`/`quote_
 *    currency` (matching the stored row shape) even though the GET query
 *    params are the shorter `base`/`quote` - two different field names for
 *    the same concept, both taken directly from fx.py's own schemas rather
 *    than invented here. `override_reason` is required non-blank
 *    (`ExchangeRateOverrideCreate`, backed by a DB CHECK constraint per
 *    that schema's docstring) - FxPanel.tsx's override form must not allow
 *    submitting a blank reason client-side either.
 *  - No `If-Match`/`ETag`: `ExchangeRateResponse` carries no `version`
 *    field and api/v1/fx.py's only mutating routes are plain `POST`s, so
 *    unlike quotes/rfqs/parts/suppliers this file needs no custom-header
 *    dance for CSRF - `post()` already attaches it automatically.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { get, post } from "../../api/client";

export interface ExchangeRateResponse {
  id: string;
  base_currency: string;
  quote_currency: string;
  rate: string;
  effective_date: string;
  source: string;
  is_manual_override: boolean;
  override_reason: string | null;
  created_by_id: string;
  retrieved_at: string;
  created_at: string;
}

export interface PageInfo {
  limit: number;
  offset: number;
  total: number;
}

export interface ExchangeRateListResponse {
  items: ExchangeRateResponse[];
  page: PageInfo;
}

/** `source` is `"synthetic_fixture"` (fixture provider, never persisted -
 * `exchange_rate_id` is `null`) or `"manual"` (a stored override row).
 * FxPanel.tsx's source badge switches on this exact string. */
export interface EffectiveExchangeRateResponse {
  base_currency: string;
  quote_currency: string;
  as_of: string;
  rate: string;
  source: string;
  is_manual_override: boolean;
  override_reason: string | null;
  effective_date: string;
  exchange_rate_id: string | null;
}

export interface FxListParams {
  // `| undefined` (not just `?`) so callers building this from a ternary
  // satisfy `exactOptionalPropertyTypes`, matching every other feature's
  // list-params interface.
  base?: string | undefined;
  quote?: string | undefined;
  limit?: number | undefined;
  offset?: number | undefined;
}

export interface FxOverrideInput {
  base_currency: string;
  quote_currency: string;
  rate: string;
  effective_date: string;
  override_reason: string;
}

function buildListQuery(params: FxListParams): string {
  const sp = new URLSearchParams();
  if (params.base) sp.set("base", params.base);
  if (params.quote) sp.set("quote", params.quote);
  sp.set("limit", String(params.limit ?? 50));
  sp.set("offset", String(params.offset ?? 0));
  return sp.toString();
}

export const fxKeys = {
  all: ["fx"] as const,
  lists: () => [...fxKeys.all, "list"] as const,
  list: (params: FxListParams) => [...fxKeys.lists(), params] as const,
  effective: (base: string, quote: string, asOf: string) =>
    [...fxKeys.all, "effective", base, quote, asOf] as const,
};

/** Paginated override-history listing - see this file's header on why
 * `as_of` is not a param here. */
export function useExchangeRates(params: FxListParams) {
  return useQuery({
    queryKey: fxKeys.list(params),
    queryFn: () => get<ExchangeRateListResponse>(`/api/v1/exchange-rates?${buildListQuery(params)}`),
    placeholderData: (prev) => prev,
  });
}

/** Effective-rate lookup - disabled until `base`/`quote`/`asOf` are all
 * non-blank, mirroring the backend's all-three-or-none branch. */
export function useEffectiveRate(base: string, quote: string, asOf: string) {
  const enabled = base.trim() !== "" && quote.trim() !== "" && asOf.trim() !== "";
  return useQuery({
    queryKey: fxKeys.effective(base, quote, asOf),
    queryFn: () => {
      const sp = new URLSearchParams({ base, quote, as_of: asOf });
      return get<EffectiveExchangeRateResponse>(`/api/v1/exchange-rates?${sp.toString()}`);
    },
    enabled,
  });
}

export function useCreateFxOverride() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (data: FxOverrideInput) => post<ExchangeRateResponse>("/api/v1/exchange-rates", data),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: fxKeys.all });
    },
  });
}
