/** Quotes data layer — TanStack Query hooks over the live API.
 *
 * Shapes mirror backend/src/app/schemas/quotes.py exactly (manual entry
 * only — extraction/matching routes are out of scope, per that module's
 * "Scope: manual entry only" note):
 *  - `GET /rfqs/{rfq_id}/quotes` returns `{ items }` — no `page` envelope
 *    (`QuoteListResponse`, unlike `RfqListResponse`/`SupplierListResponse`),
 *    so `useRfqQuotes` doesn't take/paginate offset — the whole per-RFQ
 *    quote set is returned in one call.
 *  - Every quote line's commercial fields (`unit_price`, MOQ, all
 *    tooling/setup/packaging/shipping/insurance/other/tariff/duty/customs/
 *    tax costs) are optional and nullable with **no server-side default of
 *    0** (`QuoteLineCreate` in quotes.py: "Missing stays missing"). Forms
 *    in QuotesSection.tsx must never prefill these with `"0"` — an absent
 *    field must round-trip as `null`, not a false "confirmed zero cost".
 *  - `quantity`/`moq`/`production_capacity`/price-break `min_quantity`/
 *    `max_quantity`/`setup_fee`/every per-line fixed-cost-and-tax field are
 *    6dp `QuantityString` wire strings; `unit_price` (both on the line and
 *    on each price break) is an 8dp `UnitPriceString` wire string. Both are
 *    plain JSON strings on the wire — never run through Number()/
 *    parseFloat, same rule as lib/money.ts's `isDecimalString`.
 *  - `unit_definition_id` is REQUIRED on a quote line (unlike RFQ/BOM
 *    lines, which default from the part) — quotes.py's module docstring:
 *    a quote line's `part_id` is itself optional (a supplier's raw,
 *    as-quoted line with no catalogued part at all), so there's no
 *    reliable part to default the unit *from*.
 *  - Quotes carry `ETag`/`If-Match` (api/v1/quotes.py file header), but
 *    only `PATCH /quotes/{id}` demands the header as input — `POST
 *    .../supersede` and `POST .../archive` mutate and return a fresh
 *    version without requiring it, the same split ../rfqs/api.ts documents
 *    for `POST /rfqs/{id}/status` vs `PATCH /rfqs/{id}`. `useUpdateQuote`
 *    therefore needs the same custom-header dance (re-supplying
 *    `Content-Type`/CSRF alongside `If-Match`) as `useUpdateRfq`/
 *    `useUpdateSupplier`/`useUpdatePart` — see any of those files' headers
 *    for why the CSRF token has to be re-supplied by hand once a caller
 *    sets its own `headers`.
 *  - `QuoteUpdate` (`PATCH /quotes/{id}`) only carries whole-quote metadata
 *    (`quote_number`/`quote_date`/`expiration_date`/`currency`/`notes`) —
 *    lines/price-breaks/terms are written once, atomically, at create (or
 *    supersede) time only (quotes.py module docstring "Update scope").
 *    `QuoteService.update` also only allows this while the quote is
 *    `draft`/`in_review` (`_UPDATABLE_QUOTE_STATUSES`,
 *    services/quote_service.py) — QuotesSection.tsx mirrors that gate.
 *  - `QuoteSupersedeRequest` is `QuoteCreate` minus `supplier_id` (the new
 *    quote inherits the old quote's supplier/RFQ — quotes.py module
 *    docstring "Supersede reuses the old quote's rfq_id/supplier_id").
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, get, post } from "../../api/client";
import { useAuth } from "../../auth/session";

export type QuoteStatus = "draft" | "in_review" | "confirmed" | "superseded" | "rejected";
export type QuoteSourceKind = "manual" | "extracted";
export type QuoteLineMatchStatus = "unmatched" | "auto" | "confirmed" | "rejected";

export interface QuotePriceBreakInput {
  min_quantity: string;
  max_quantity: string | null;
  unit_price: string;
  setup_fee: string | null;
  tier_index: number | null;
}

export interface QuotePriceBreakResponse extends QuotePriceBreakInput {
  id: string;
}

export interface QuoteLineInput {
  matched_rfq_line_id: string | null;
  part_id: string | null;
  quoted_part_number: string | null;
  quoted_mpn: string | null;
  description: string | null;
  quantity: string;
  unit_definition_id: string;
  unit_price: string | null;
  moq: string | null;
  tooling_cost: string | null;
  setup_cost: string | null;
  documentation_cost: string | null;
  packaging_cost: string | null;
  shipping_cost: string | null;
  handling_cost: string | null;
  insurance_cost: string | null;
  other_fixed_cost: string | null;
  tariff_amount: string | null;
  duty_amount: string | null;
  customs_fee: string | null;
  tax_amount: string | null;
  country_of_origin: string | null;
  lead_time_days: number | null;
  production_capacity: string | null;
  notes: string | null;
  price_breaks: QuotePriceBreakInput[];
}

export interface QuoteLineResponse
  extends Omit<QuoteLineInput, "price_breaks"> {
  id: string;
  line_number: number;
  match_status: QuoteLineMatchStatus;
  price_breaks: QuotePriceBreakResponse[];
}

export interface QuoteTermsInput {
  payment_terms: string | null;
  payment_terms_days: number | null;
  incoterm: string | null;
  shipping_terms: string | null;
  warranty_terms: string | null;
  validity_days: number | null;
  exceptions: string | null;
  exclusions: string | null;
  notes: string | null;
}

export type QuoteTermsResponse = QuoteTermsInput;

export interface QuoteResponse {
  id: string;
  rfq_id: string;
  supplier_id: string;
  quote_number: string | null;
  quote_date: string;
  expiration_date: string | null;
  currency: string;
  status: QuoteStatus;
  source: QuoteSourceKind;
  notes: string | null;
  superseded_by_id: string | null;
  is_archived: boolean;
  archived_at: string | null;
  archive_reason: string | null;
  version: number;
  created_at: string;
  updated_at: string;
  lines: QuoteLineResponse[];
  terms: QuoteTermsResponse | null;
}

/** `GET /rfqs/{rfq_id}/quotes` items — no `lines`/`terms` (see this file's
 * header: `QuoteListResponse` carries no `page` either). */
export interface QuoteSummaryResponse {
  id: string;
  rfq_id: string;
  supplier_id: string;
  quote_number: string | null;
  quote_date: string;
  expiration_date: string | null;
  currency: string;
  status: QuoteStatus;
  source: QuoteSourceKind;
  superseded_by_id: string | null;
  is_archived: boolean;
  version: number;
  created_at: string;
  updated_at: string;
  line_count: number;
}

export interface QuoteListResponse {
  items: QuoteSummaryResponse[];
}

export interface QuoteCreateInput {
  supplier_id: string;
  quote_number: string | null;
  quote_date: string;
  expiration_date: string | null;
  currency: string;
  notes: string | null;
  lines: QuoteLineInput[];
  terms: QuoteTermsInput | null;
}

/** `POST /quotes/{id}/supersede` body — see this file's header. */
export type QuoteSupersedeInput = Omit<QuoteCreateInput, "supplier_id">;

export interface QuoteUpdateInput {
  quote_number?: string | null;
  quote_date?: string;
  expiration_date?: string | null;
  currency?: string;
  notes?: string | null;
}

export const quoteKeys = {
  all: ["quotes"] as const,
  rfqLists: () => [...quoteKeys.all, "rfq-list"] as const,
  rfqList: (rfqId: string, includeSuperseded: boolean) =>
    [...quoteKeys.rfqLists(), rfqId, includeSuperseded] as const,
  details: () => [...quoteKeys.all, "detail"] as const,
  detail: (id: string) => [...quoteKeys.details(), id] as const,
};

export function useRfqQuotes(rfqId: string | null, includeSuperseded = true) {
  return useQuery({
    queryKey: quoteKeys.rfqList(rfqId ?? "", includeSuperseded),
    queryFn: () =>
      get<QuoteListResponse>(
        `/api/v1/rfqs/${rfqId}/quotes?include_superseded=${String(includeSuperseded)}`,
      ),
    enabled: rfqId !== null,
  });
}

export function useQuote(id: string | null) {
  return useQuery({
    queryKey: quoteKeys.detail(id ?? ""),
    queryFn: () => get<QuoteResponse>(`/api/v1/quotes/${id}`),
    enabled: id !== null,
  });
}

export interface CreateQuoteVars {
  rfqId: string;
  data: QuoteCreateInput;
}

export function useCreateQuote() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ rfqId, data }: CreateQuoteVars) =>
      post<QuoteResponse>(`/api/v1/rfqs/${rfqId}/quotes`, data),
    onSuccess: (created) => {
      void queryClient.invalidateQueries({ queryKey: quoteKeys.rfqLists() });
      queryClient.setQueryData(quoteKeys.detail(created.id), created);
    },
  });
}

export interface UpdateQuoteVars {
  id: string;
  version: number;
  data: QuoteUpdateInput;
}

export function useUpdateQuote() {
  const { session } = useAuth();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, version, data }: UpdateQuoteVars) =>
      api<QuoteResponse>("PATCH", `/api/v1/quotes/${id}`, data, {
        headers: {
          "Content-Type": "application/json",
          "If-Match": `"${version}"`,
          ...(session?.csrf_token ? { "X-CSRF-Token": session.csrf_token } : {}),
        },
      }),
    onSuccess: (updated) => {
      void queryClient.invalidateQueries({ queryKey: quoteKeys.rfqLists() });
      queryClient.setQueryData(quoteKeys.detail(updated.id), updated);
    },
  });
}

export interface SupersedeQuoteVars {
  id: string;
  data: QuoteSupersedeInput;
}

export function useSupersedeQuote() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, data }: SupersedeQuoteVars) =>
      post<QuoteResponse>(`/api/v1/quotes/${id}/supersede`, data),
    onSuccess: (created) => {
      void queryClient.invalidateQueries({ queryKey: quoteKeys.rfqLists() });
      queryClient.setQueryData(quoteKeys.detail(created.id), created);
      // The superseded predecessor's own status/superseded_by_id flips
      // server-side too (QuoteService.supersede) — invalidate every cached
      // quote detail rather than track which id was the predecessor.
      void queryClient.invalidateQueries({ queryKey: quoteKeys.details() });
    },
  });
}

export interface ArchiveQuoteVars {
  id: string;
  reason: string;
}

export function useArchiveQuote() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, reason }: ArchiveQuoteVars) =>
      post<QuoteResponse>(`/api/v1/quotes/${id}/archive`, { reason }),
    onSuccess: (updated) => {
      void queryClient.invalidateQueries({ queryKey: quoteKeys.rfqLists() });
      queryClient.setQueryData(quoteKeys.detail(updated.id), updated);
    },
  });
}
