import type {
  AgentPerformance,
  AgentSessionReplay,
  AuditTrail,
  ApiEnvelope,
  AppStateCount,
  ApplicationSummary,
  ComplianceView,
  LedgerHealth,
  RecentEvent,
  ResourceDefinition,
  ToolDefinition
} from "./types";

const API_BASE =
  import.meta.env.VITE_API_BASE_URL?.replace(/\/$/, "") ?? "http://127.0.0.1:8000/api/v1";
const API_KEY = import.meta.env.VITE_LEDGER_API_KEY;
const TOKEN_STORAGE_KEY = "ledger_dashboard_access_token";

export class LedgerApiError extends Error {
  status: number;
  errorType?: string;
  suggestedAction?: string;

  constructor(message: string, status: number, errorType?: string, suggestedAction?: string) {
    super(message);
    this.status = status;
    this.errorType = errorType;
    this.suggestedAction = suggestedAction;
  }
}

export type LoginResponse = {
  access_token: string;
  token_type: string;
  expires_at: string;
  user: {
    username: string;
    role: string;
    issued_at: string;
  };
  allowed_commands: string[];
};

export type MeResponse = {
  username: string;
  role: string;
  issued_at: string;
  expires_at: string;
  allowed_commands: string[];
};

export type AuthAuditRow = {
  audit_id: number;
  username: string | null;
  role: string | null;
  action: string;
  success: boolean;
  ip_address: string | null;
  user_agent: string | null;
  details: Record<string, unknown>;
  created_at: string;
};

function readStoredToken(): string | null {
  return localStorage.getItem(TOKEN_STORAGE_KEY);
}

function writeStoredToken(value: string): void {
  localStorage.setItem(TOKEN_STORAGE_KEY, value);
}

export function clearStoredToken(): void {
  localStorage.removeItem(TOKEN_STORAGE_KEY);
}

async function apiRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers ?? {});
  headers.set("Content-Type", "application/json");
  if (API_KEY) {
    headers.set("x-api-key", API_KEY);
  }

  const token = readStoredToken();
  if (token) {
    headers.set("authorization", `Bearer ${token}`);
  }

  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers
  });

  const contentType = response.headers.get("content-type") ?? "";
  const body = contentType.includes("application/json")
    ? ((await response.json()) as ApiEnvelope<T>)
    : null;

  if (!response.ok) {
    const message = body?.error?.message ?? `Request failed with status ${response.status}`;
    throw new LedgerApiError(
      message,
      response.status,
      body?.error?.error_type,
      body?.error?.suggested_action
    );
  }

  if (!body || !body.ok || !body.result) {
    throw new LedgerApiError("Malformed API response", response.status);
  }

  return body.result;
}

export async function login(username: string, password: string): Promise<LoginResponse> {
  const result = await apiRequest<LoginResponse>("/auth/login", {
    method: "POST",
    body: JSON.stringify({ username, password })
  });
  writeStoredToken(result.access_token);
  return result;
}

export async function fetchMe(): Promise<MeResponse> {
  return await apiRequest<MeResponse>("/auth/me");
}

export async function fetchAuthAudit(limit = 100): Promise<AuthAuditRow[]> {
  const result = await apiRequest<{ items: AuthAuditRow[] }>(`/auth/audit?limit=${limit}`);
  return result.items;
}

export async function fetchTools(): Promise<ToolDefinition[]> {
  const result = await apiRequest<{ tools: ToolDefinition[] }>("/tools");
  return result.tools;
}

export async function fetchResources(): Promise<ResourceDefinition[]> {
  const result = await apiRequest<{ resources: ResourceDefinition[] }>("/resources");
  return result.resources;
}

export async function runCommand(
  toolName: string,
  argumentsPayload: Record<string, unknown>
): Promise<Record<string, unknown>> {
  const result = await apiRequest<Record<string, unknown>>(`/commands/${toolName}`, {
    method: "POST",
    body: JSON.stringify({ arguments: argumentsPayload })
  });
  return result;
}

export async function bootstrapDemo(applicationId?: string): Promise<Record<string, unknown>> {
  return await apiRequest<Record<string, unknown>>("/bootstrap/demo", {
    method: "POST",
    body: JSON.stringify({ application_id: applicationId || undefined })
  });
}

export async function fetchLedgerHealth(): Promise<LedgerHealth> {
  return await apiRequest<LedgerHealth>("/ledger/health");
}

export async function fetchApplicationStates(): Promise<AppStateCount[]> {
  const result = await apiRequest<{ states: AppStateCount[] }>("/application-states");
  return result.states;
}

export async function fetchApplication(applicationId: string): Promise<ApplicationSummary> {
  return await apiRequest<ApplicationSummary>(`/applications/${applicationId}`);
}

export async function fetchCompliance(
  applicationId: string,
  asOf?: string
): Promise<ComplianceView> {
  const query = asOf ? `?as_of=${encodeURIComponent(asOf)}` : "";
  return await apiRequest<ComplianceView>(`/applications/${applicationId}/compliance${query}`);
}

export async function fetchApplicationAuditTrail(applicationId: string): Promise<AuditTrail> {
  return await apiRequest<AuditTrail>(`/applications/${applicationId}/audit-trail`);
}

export async function fetchApplicationEvents(
  applicationId: string,
  limit = 1000
): Promise<RecentEvent[]> {
  try {
    const result = await apiRequest<{ items: RecentEvent[] }>(
      `/applications/${applicationId}/events?limit=${limit}`
    );
    return result.items;
  } catch (error) {
    if (!(error instanceof LedgerApiError) || error.status !== 404) {
      throw error;
    }

    // Backward-compatible fallback for older API servers that do not expose
    // /applications/{id}/events yet.
    const recent = await fetchRecentEvents(Math.max(limit, 500));
    const streamIds = new Set<string>([
      `loan-${applicationId}`,
      `compliance-${applicationId}`,
      `audit-application-${applicationId}`
    ]);

    for (const event of recent) {
      const payloadAppId = String(event.payload.application_id ?? "");
      const payloadEntityType = String(event.payload.entity_type ?? "");
      const payloadEntityId = String(event.payload.entity_id ?? "");
      if (
        payloadAppId === applicationId ||
        event.stream_id.includes(applicationId) ||
        (payloadEntityType === "application" && payloadEntityId === applicationId)
      ) {
        streamIds.add(event.stream_id);
      }
    }

    const streamResults = await Promise.all(
      Array.from(streamIds).map(async (streamId) => {
        try {
          const stream = await apiRequest<{ items: RecentEvent[] }>(
            `/streams/${encodeURIComponent(streamId)}?from_position=1&limit=1000`
          );
          return stream.items;
        } catch {
          return [] as RecentEvent[];
        }
      })
    );

    const deduped = new Map<string, RecentEvent>();
    for (const event of streamResults.flat()) {
      deduped.set(event.event_id, event);
    }
    return Array.from(deduped.values()).sort(
      (left, right) => left.global_position - right.global_position
    );
  }
}

export async function fetchAgentPerformance(agentId: string): Promise<AgentPerformance> {
  return await apiRequest<AgentPerformance>(`/agents/${agentId}/performance`);
}

export async function fetchAgentSession(
  agentId: string,
  sessionId: string
): Promise<AgentSessionReplay> {
  return await apiRequest<AgentSessionReplay>(`/agents/${agentId}/sessions/${sessionId}`);
}

export async function fetchResourceByUri(uri: string): Promise<unknown> {
  let parsed: URL;
  try {
    parsed = new URL(uri);
  } catch {
    throw new LedgerApiError("Invalid resource URI format.", 422, "ValidationError", "use_ledger_uri");
  }

  if (parsed.protocol !== "ledger:") {
    throw new LedgerApiError(
      `Unsupported resource scheme '${parsed.protocol}'.`,
      422,
      "ValidationError",
      "use_ledger_scheme"
    );
  }

  const parts = parsed.pathname.split("/").filter(Boolean);
  if (parsed.hostname === "applications") {
    if (parts.length === 1) {
      return await fetchApplication(parts[0]);
    }
    if (parts.length === 2 && parts[1] === "compliance") {
      const asOf = parsed.searchParams.get("as_of") ?? undefined;
      return await fetchCompliance(parts[0], asOf);
    }
    if (parts.length === 2 && parts[1] === "audit-trail") {
      return await fetchApplicationAuditTrail(parts[0]);
    }
  }

  if (parsed.hostname === "agents") {
    if (parts.length === 2 && parts[1] === "performance") {
      return await fetchAgentPerformance(parts[0]);
    }
    if (parts.length === 3 && parts[1] === "sessions") {
      return await fetchAgentSession(parts[0], parts[2]);
    }
  }

  if (parsed.hostname === "ledger" && parts.length === 1 && parts[0] === "health") {
    return await fetchLedgerHealth();
  }

  throw new LedgerApiError(
    `Unknown resource URI '${uri}'.`,
    404,
    "UnknownResource",
    "use_list_resources"
  );
}

export async function fetchRecentEvents(limit = 20): Promise<RecentEvent[]> {
  const result = await apiRequest<{ items: RecentEvent[] }>(`/events/recent?limit=${limit}`);
  return result.items;
}

export async function fetchApplications(limit = 20): Promise<ApplicationSummary[]> {
  const result = await apiRequest<{ items: ApplicationSummary[] }>(`/applications?limit=${limit}`);
  return result.items;
}
