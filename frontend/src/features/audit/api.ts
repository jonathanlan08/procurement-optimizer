/** Audit-log data layer - TanStack Query hooks against the FROZEN wire
 * contract given in this task's brief. The backend is being built
 * concurrently (see ../reports/api.ts's file header for the same
 * "code to the frozen contract" situation) - this file documents the two
 * places the contract leaves a shape ambiguous:
 *  - **`actor_user_id`/`explanation` are typed nullable.** The contract
 *    lists both as plain fields (no `?`), but an audit trail necessarily
 *    also records system-initiated changes (scheduled jobs, migrations)
 *    with no human actor, and not every event necessarily carries a
 *    human-readable explanation - nullable is the safer reading given the
 *    UI spec's own "Show raw UUIDs for actors ... do not fake names"
 *    instruction, which only makes sense if a row can legitimately have no
 *    actor to show a UUID for. AuditPage.tsx renders `null` as "system"
 *    for the actor and "No explanation recorded." for the explanation,
 *    never a fabricated value.
 *  - **`before_state`/`after_state` are typed as `Record<string, unknown> |
 *    null`** - genuinely untyped JSONB snapshots (no fixed shape is
 *    possible across every `entity_type` this log can cover), same
 *    "don't invent a shape" treatment ../reports/api.ts's header documents
 *    for `parameters`. `null` covers a create event's `before_state` and a
 *    delete/archive event's `after_state`.
 *  - `request_id` is the one field the contract itself marks optional
 *    (`request_id?`) - typed `string | null` here since a present-but-empty
 *    key vs. an absent key are indistinguishable once through `JSON.parse`
 *    either way.
 */

import { useQuery } from "@tanstack/react-query";
import { get } from "../../api/client";

export interface AuditEventResponse {
  id: string;
  occurred_at: string;
  event_type: string;
  entity_type: string;
  entity_id: string | null; // backend AuditEventResponse.entity_id is nullable
  actor_user_id: string | null;
  explanation: string | null;
  before_state: Record<string, unknown> | null;
  after_state: Record<string, unknown> | null;
  request_id?: string | null;
}

export interface AuditEventListResponse {
  items: AuditEventResponse[];
  next_cursor: string | null;
}

export interface AuditEventListParams {
  event_type?: string[] | undefined;
  entity_type?: string | undefined;
  entity_id?: string | undefined;
  actor_user_id?: string | undefined;
  from?: string | undefined;
  to?: string | undefined;
  limit?: number | undefined;
  cursor?: string | undefined;
}

function buildQuery(params: AuditEventListParams): string {
  const sp = new URLSearchParams();
  for (const et of params.event_type ?? []) sp.append("event_type", et);
  if (params.entity_type) sp.set("entity_type", params.entity_type);
  if (params.entity_id) sp.set("entity_id", params.entity_id);
  if (params.actor_user_id) sp.set("actor_user_id", params.actor_user_id);
  if (params.from) sp.set("from", params.from);
  if (params.to) sp.set("to", params.to);
  sp.set("limit", String(params.limit ?? 50));
  if (params.cursor) sp.set("cursor", params.cursor);
  return sp.toString();
}

export const auditKeys = {
  all: ["audit-events"] as const,
  list: (params: AuditEventListParams) => [...auditKeys.all, "list", params] as const,
};

export function useAuditEvents(params: AuditEventListParams) {
  return useQuery({
    queryKey: auditKeys.list(params),
    queryFn: () => get<AuditEventListResponse>(`/api/v1/audit-events?${buildQuery(params)}`),
  });
}
