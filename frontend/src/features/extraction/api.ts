/** Extraction-review + part-matching data layer — TanStack Query hooks over
 * the live API.
 *
 * Shapes mirror backend/src/app/schemas/extractions.py + matching.py exactly
 * (backend/src/app/api/v1/extractions.py + matching.py):
 *  - `GET /quote-documents/{id}/extraction-runs` / `.../fields` both return
 *    `{ items }` — no `page` envelope, same "whole set in one call" shape
 *    ../quotes/api.ts's file header documents for `QuoteListResponse` (a
 *    document/run realistically has a handful of runs/fields, not pages of
 *    them).
 *  - **`start_run` always executes synchronously** (extraction_service.py's
 *    own module docstring: "JOB_RUNNER=inline... the only shape this v0.1
 *    build implements, not a test-only special case") — `POST
 *    /quote-documents/{id}/extraction-runs` returns `201` with the
 *    *completed* `ExtractionRunResponse` already in a terminal-ish state
 *    (`extracted`/`needs_review`/`ready`/`failed`), never `queued`/
 *    `running` from the caller's point of view. `useStartExtractionRun`
 *    therefore needs no polling.
 *  - `overall_confidence`/`confidence` are decimal wire strings (never
 *    through Number()/parseFloat, lib/money.ts's rule) — `band` is already
 *    the pre-computed `"high"|"medium"|"low"` string
 *    (`app.domain.confidence.ConfidenceBand`, thresholds 0.95/0.60,
 *    MASTER.md's own mirrored table), so ReviewPane.tsx never recomputes a
 *    band from the raw confidence itself.
 *  - **`PATCH /extraction-runs/{id}/fields/{fid}` dispatches confirm vs.
 *    correct from one combined body** (extractions.py's own module
 *    docstring): a present `normalized_value` corrects (and always
 *    confirms as a side effect); its absence with `is_confirmed: true`
 *    plainly confirms. `usePatchExtractionField` exposes both call shapes
 *    through the one mutation, matching the one route.
 *  - **`POST /extraction-runs/{id}/confirm` (materialize) returns a
 *    `QuoteResponse`** (the same shape ../quotes/api.ts's `QuoteResponse`
 *    already types) — imported from there rather than re-declared, and its
 *    success invalidates `quoteKeys.rfqLists()`/`quoteKeys.detail()` too
 *    (imported key builders, not private symbols) so the RFQ drawer's own
 *    QuotesSection picks up the newly-materialized quote without a manual
 *    refetch once ReviewPane.tsx closes — the same cross-feature
 *    invalidation-on-a-known-read pattern already used throughout this
 *    app's mutations for their *own* feature's queries, extended here to
 *    the one external read this action is documented to affect.
 *  - **Materialize's `409`** (unconfirmed low-band fields) carries one
 *    `ErrorDetail` per blocking field
 *    (`issue: "low-confidence field is not confirmed"`, `field:
 *    <field_path>`) — `ApiErrorBanner` already renders `message` +
 *    every `details[]` entry generically, so ReviewPane.tsx's "409 callout
 *    listing which fields block" is that same banner, not bespoke parsing.
 *
 * **Matching hooks (`/quotes/{id}/match(es)`, `/quote-lines/{id}/match`)
 * are colocated here, not a separate `features/matching/` folder** — the
 * task brief describes match-candidate review as part of the same
 * post-materialize surface ReviewPane.tsx owns ("After materialize: link
 * to the quote + match candidates section"), and no standalone matching
 * folder is in this task's file list.
 *  - **"Confirm/Reject" per candidate is a judgment call over the real
 *    route shape**, documented here since the literal API has no
 *    per-candidate confirm/reject route at all (`services/
 *    matching_service.py`'s own module docstring: confirm/unmatch are
 *    addressed by *quote line*, with an explicit `rfq_line_id`, not a
 *    `PartMatchCandidate` id). "Confirm" on a candidate row calls
 *    `useConfirmQuoteLineMatch` with that candidate's own `rfq_line_id`;
 *    "Reject" only renders on the line's currently-selected/confirmed
 *    candidate and calls `useUnmatchQuoteLineMatch` (clears the line back
 *    to unmatched) — the closest faithful mapping onto the real,
 *    line-addressed confirm/unmatch pair.
 *  - **Auto-confirmed exact matches** (`match_status === "auto"`,
 *    `matching_service.py`'s "Auto-confirmation (strategy internal_pn
 *    only)") render a quiet "auto-matched" chip instead of action buttons —
 *    `human_confirmed` stays `false` on that line's winning candidate even
 *    though it is already `is_selected`, precisely so the UI can tell "the
 *    matcher decided this" apart from "a human confirmed this"
 *    (`match_status === "confirmed"`).
 *
 * Vendor/supplier *scoring* (`SupplierScore`, `app/domain/scoring/`) is a
 * different domain concept from the part-*matching* candidates this file
 * owns — that lives in ../comparison/api.ts instead (see that file's header
 * for a documented backend gap: ranked scoring has no implemented route
 * yet), matching the task brief's own split (item 2 = this file's
 * extraction+matching review surface; item 3 = the comparison workspace's
 * scoring section).
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, get, post } from "../../api/client";
import { quoteKeys, type QuoteResponse } from "../quotes/api";

// -- extraction runs -----------------------------------------------------

export type ExtractionRunState =
  | "queued"
  | "running"
  | "failed_transient"
  | "failed"
  | "extracted"
  | "needs_review"
  | "ready"
  | "materialized"
  | "superseded";

export interface ExtractionRunErrorInfo {
  code: string;
  message: string;
}

export interface ExtractionRunResponse {
  id: string;
  document_id: string;
  run_number: number;
  state: ExtractionRunState;
  provider: string;
  provider_model: string | null;
  simulated: boolean;
  schema_version: string;
  prompt_version: string;
  overall_confidence: string | null;
  fields_requiring_review: number | null;
  injection_suspected: boolean;
  error: ExtractionRunErrorInfo | null;
  started_at: string;
  finished_at: string | null;
  superseded_at: string | null;
}

export interface ExtractionRunListResponse {
  items: ExtractionRunResponse[];
}

// -- extraction fields ----------------------------------------------------

export type ConfidenceBand = "high" | "medium" | "low";

/** `target_entity`: header-level ("quote"), terms ("quote_terms"), or a
 * per-line field ("quote_line", with `target_line_index` set) — see
 * `services/extraction_service.py`'s `_build_fields`. */
export type FieldTargetEntity = "quote" | "quote_terms" | "quote_line";

export interface ExtractionFieldResponse {
  id: string;
  extraction_run_id: string;
  field_path: string;
  target_entity: FieldTargetEntity;
  target_line_index: number | null;
  field_name: string;
  raw_value: string | null;
  normalized_value: string | null;
  value_type: string;
  confidence: string;
  band: ConfidenceBand;
  requires_confirmation: boolean;
  is_confirmed: boolean;
  confirmed_by_id: string | null;
  confirmed_at: string | null;
  source_page: number | null;
  source_bbox: Record<string, unknown> | null;
  injection_flagged: boolean;
}

export interface ExtractionFieldListResponse {
  items: ExtractionFieldResponse[];
}

export interface ExtractionFieldPatchInput {
  normalized_value?: string | null;
  is_confirmed: boolean;
  reason?: string | null;
}

export interface ExtractionMaterializeInput {
  supplier_id: string;
}

export const extractionKeys = {
  all: ["extraction"] as const,
  runLists: () => [...extractionKeys.all, "run-list"] as const,
  runList: (documentId: string) => [...extractionKeys.runLists(), documentId] as const,
  runs: () => [...extractionKeys.all, "run"] as const,
  run: (id: string) => [...extractionKeys.runs(), id] as const,
  fields: (runId: string) => [...extractionKeys.run(runId), "fields"] as const,
};

export function useDocumentExtractionRuns(documentId: string | null) {
  return useQuery({
    queryKey: extractionKeys.runList(documentId ?? ""),
    queryFn: () =>
      get<ExtractionRunListResponse>(`/api/v1/quote-documents/${documentId}/extraction-runs`),
    enabled: documentId !== null,
  });
}

export function useExtractionRun(runId: string | null) {
  return useQuery({
    queryKey: extractionKeys.run(runId ?? ""),
    queryFn: () => get<ExtractionRunResponse>(`/api/v1/extraction-runs/${runId}`),
    enabled: runId !== null,
  });
}

export function useExtractionFields(runId: string | null) {
  return useQuery({
    queryKey: extractionKeys.fields(runId ?? ""),
    queryFn: () => get<ExtractionFieldListResponse>(`/api/v1/extraction-runs/${runId}/fields`),
    enabled: runId !== null,
  });
}

export interface StartExtractionRunVars {
  documentId: string;
}

export function useStartExtractionRun() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ documentId }: StartExtractionRunVars) =>
      post<ExtractionRunResponse>(`/api/v1/quote-documents/${documentId}/extraction-runs`),
    onSuccess: (created, vars) => {
      void queryClient.invalidateQueries({ queryKey: extractionKeys.runList(vars.documentId) });
      queryClient.setQueryData(extractionKeys.run(created.id), created);
    },
  });
}

export interface PatchExtractionFieldVars {
  runId: string;
  fieldId: string;
  data: ExtractionFieldPatchInput;
}

export function usePatchExtractionField() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ runId, fieldId, data }: PatchExtractionFieldVars) =>
      api<ExtractionFieldResponse>(
        "PATCH",
        `/api/v1/extraction-runs/${runId}/fields/${fieldId}`,
        data,
      ),
    onSuccess: (_result, vars) => {
      void queryClient.invalidateQueries({ queryKey: extractionKeys.fields(vars.runId) });
      // A field confirm/correct can flip the run needs_review -> ready
      // (ExtractionService._recompute_run_state) — refetch the run header too.
      void queryClient.invalidateQueries({ queryKey: extractionKeys.run(vars.runId) });
    },
  });
}

/** `POST /extraction-runs/{id}/fields/confirm-all` (2026-08 audit
 * remediation, wave B) — bulk counterpart to `usePatchExtractionField`'s
 * per-field confirm. No request body; the response is the run detail
 * itself (`ExtractionRunResponse`, not a field — see
 * `extractions.py::confirm_all_extraction_fields`'s own docstring for why),
 * so on success this both seeds the run query cache directly with that
 * response (the same "already have the fresh value, skip a refetch"
 * shortcut `useStartExtractionRun` uses above) and invalidates the fields
 * list, whose individual `is_confirmed` flags the run response doesn't
 * carry. */
export function useConfirmAllExtractionFields() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (runId: string) =>
      post<ExtractionRunResponse>(`/api/v1/extraction-runs/${runId}/fields/confirm-all`),
    onSuccess: (run, runId) => {
      queryClient.setQueryData(extractionKeys.run(runId), run);
      void queryClient.invalidateQueries({ queryKey: extractionKeys.fields(runId) });
    },
  });
}

export interface MaterializeExtractionRunVars {
  runId: string;
  data: ExtractionMaterializeInput;
}

export function useMaterializeExtractionRun() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ runId, data }: MaterializeExtractionRunVars) =>
      post<QuoteResponse>(`/api/v1/extraction-runs/${runId}/confirm`, data),
    onSuccess: (quote, vars) => {
      void queryClient.invalidateQueries({ queryKey: extractionKeys.run(vars.runId) });
      // See this file's header: materialize's effect on the RFQ drawer's
      // own QuotesSection is a documented, deliberate cross-feature
      // invalidation, not an accidental reach into another feature's cache.
      void queryClient.invalidateQueries({ queryKey: quoteKeys.rfqLists() });
      queryClient.setQueryData(quoteKeys.detail(quote.id), quote);
    },
  });
}

// -- part matching ---------------------------------------------------------

export type MatchStrategy = "internal_pn" | "mpn" | "normalized_text" | "alternative" | "fuzzy";
export type QuoteLineMatchStatus = "unmatched" | "auto" | "confirmed" | "rejected";

export interface MatchCandidateResponse {
  id: string;
  rfq_line_id: string;
  part_id: string;
  strategy: MatchStrategy;
  confidence: string;
  explanation: string | null;
  rank: number;
  is_selected: boolean;
  human_confirmed: boolean;
  confirmed_by_id: string | null;
  confirmed_at: string | null;
}

export interface QuoteLineMatchesResponse {
  quote_line_id: string;
  match_status: QuoteLineMatchStatus;
  matched_rfq_line_id: string | null;
  part_id: string | null;
  candidates: MatchCandidateResponse[];
}

export interface QuoteMatchesResponse {
  items: QuoteLineMatchesResponse[];
}

export const matchKeys = {
  all: ["matches"] as const,
  quote: (quoteId: string) => [...matchKeys.all, quoteId] as const,
};

export function useQuoteMatches(quoteId: string | null) {
  return useQuery({
    queryKey: matchKeys.quote(quoteId ?? ""),
    queryFn: () => get<QuoteMatchesResponse>(`/api/v1/quotes/${quoteId}/matches`),
    enabled: quoteId !== null,
  });
}

export function useGenerateQuoteMatches() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (quoteId: string) => post<QuoteMatchesResponse>(`/api/v1/quotes/${quoteId}/match`),
    onSuccess: (result, quoteId) => {
      queryClient.setQueryData(matchKeys.quote(quoteId), result);
    },
  });
}

export interface ConfirmQuoteLineMatchVars {
  quoteId: string;
  quoteLineId: string;
  rfqLineId: string;
  reason?: string | undefined;
}

export function useConfirmQuoteLineMatch() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ quoteLineId, rfqLineId, reason }: ConfirmQuoteLineMatchVars) =>
      post(`/api/v1/quote-lines/${quoteLineId}/match`, {
        rfq_line_id: rfqLineId,
        confirmed: true,
        reason: reason ?? null,
      }),
    onSuccess: (_result, vars) => {
      void queryClient.invalidateQueries({ queryKey: matchKeys.quote(vars.quoteId) });
    },
  });
}

export interface UnmatchQuoteLineVars {
  quoteId: string;
  quoteLineId: string;
  reason?: string | undefined;
}

export function useUnmatchQuoteLineMatch() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ quoteLineId, reason }: UnmatchQuoteLineVars) =>
      api("DELETE", `/api/v1/quote-lines/${quoteLineId}/match`, reason ? { reason } : undefined),
    onSuccess: (_result, vars) => {
      void queryClient.invalidateQueries({ queryKey: matchKeys.quote(vars.quoteId) });
    },
  });
}
