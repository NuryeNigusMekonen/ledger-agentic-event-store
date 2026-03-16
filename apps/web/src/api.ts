import type {
  AgentPerformance,
  ApiEnvelope,
  AppStateCount,
  ApplicationSummary,
  ComplianceView,
  LedgerHealth,
  RecentEvent,
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

export async function fetchCompliance(applicationId: string): Promise<ComplianceView> {
  return await apiRequest<ComplianceView>(`/applications/${applicationId}/compliance`);
}

export async function fetchAgentPerformance(agentId: string): Promise<AgentPerformance> {
  return await apiRequest<AgentPerformance>(`/agents/${agentId}/performance`);
}

export async function fetchRecentEvents(limit = 20): Promise<RecentEvent[]> {
  const result = await apiRequest<{ items: RecentEvent[] }>(`/events/recent?limit=${limit}`);
  return result.items;
}

export async function fetchApplications(limit = 20): Promise<ApplicationSummary[]> {
  const result = await apiRequest<{ items: ApplicationSummary[] }>(`/applications?limit=${limit}`);
  return result.items;
}
