export interface Journey {
  schema_version: string;
  lead_id: string;
  lifecycle_state: string;
  lifecycle_version: number;
  transitions: {transition_id: string; to_state: string; reason_code: string; occurred_at: string; lifecycle_version: number}[];
  channel_health: {channel: string; address_ref: string; state: string; reason_code: string}[];
  suppressions: {suppression_id: string; scope: string; channel: string | null; reason: string}[];
  exposures: {exposure_id: string; campaign_id: string; channel: string; status: string; reserved_at: string; engagement_outcome: string; negative_outcome: string}[];
  next_cursor: string | null;
}
export interface NextAction {
  eligible: boolean;
  reason_codes: string[];
  selected: {campaign_id: string; channel: string} | null;
  next_eligible_at: string | null;
}
type AuthorityHealth = Journey['channel_health'][number] & {
  suppression?: {suppression_id: string; scope: string; reason: string} | null;
};
type AuthorityJourney = Omit<Journey, 'suppressions' | 'channel_health'> & {
  channel_health: AuthorityHealth[];
  suppressions?: Journey['suppressions'];
};

// The MCR journey authority (GET /platform/v1/leads/{lead_id}/journey) nests
// each suppression under the channel-health row it explains; flatten them
// once, keeping a global suppression channel-less.
export function normalizeJourney(raw: AuthorityJourney): Journey {
  if (Array.isArray(raw.suppressions)) return raw as Journey;
  const seen = new Set<string>();
  const suppressions: Journey['suppressions'] = [];
  for (const row of raw.channel_health ?? []) {
    const item = row.suppression;
    if (!item || seen.has(item.suppression_id)) continue;
    seen.add(item.suppression_id);
    suppressions.push({suppression_id: item.suppression_id, scope: item.scope, channel: item.scope === 'global' ? null : row.channel, reason: item.reason});
  }
  return {...raw, channel_health: raw.channel_health ?? [], suppressions};
}

export interface JourneyService {
  journey(leadId: string, cursor?: string, signal?: AbortSignal): Promise<Journey>;
  nextAction(leadId: string, signal?: AbortSignal): Promise<NextAction>;
}

// The host supplies an authenticated transport. Never persist credentials or
// infer a tenant/public lead identity from the desktop's synthetic fixture.
export function createJourneyService(tenantId: string, authenticatedFetch: typeof fetch): JourneyService {
  async function read<T>(leadId: string, resource: string, signal?: AbortSignal): Promise<T> {
    const response = await authenticatedFetch(`/platform/v1/leads/${encodeURIComponent(leadId)}/${resource}`, {
      method: 'GET', signal, headers: {'Accept':'application/json', 'X-Tenant-ID':tenantId, 'X-Correlation-ID':crypto.randomUUID()},
    });
    if (!response.ok) {
      const body = await response.json().catch(() => null);
      throw new Error(body?.error?.message ?? `Lead read unavailable (${response.status})`);
    }
    return response.json() as Promise<T>;
  }
  return {
    journey: async (leadId, cursor, signal) => normalizeJourney(await read<AuthorityJourney>(leadId, `journey?limit=100${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ''}`, signal)),
    nextAction: (leadId, signal) => read<NextAction>(leadId, 'next-action', signal),
  };
}
