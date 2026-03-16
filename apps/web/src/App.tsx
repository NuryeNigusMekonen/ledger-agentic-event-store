import { useCallback, useEffect, useMemo, useState, type FormEvent } from "react";

import {
  bootstrapDemo,
  clearStoredToken,
  fetchAgentPerformance,
  fetchApplication,
  fetchApplications,
  fetchApplicationStates,
  fetchAuthAudit,
  fetchCompliance,
  fetchLedgerHealth,
  fetchMe,
  fetchRecentEvents,
  fetchTools,
  LedgerApiError,
  login,
  runCommand,
  type AuthAuditRow,
  type MeResponse
} from "./api";
import type {
  AgentPerformance,
  AppStateCount,
  ApplicationSummary,
  ComplianceView,
  LedgerHealth,
  RecentEvent,
  ToolDefinition
} from "./types";

const DEFAULT_COMMAND_PAYLOAD = {
  submit_application: {
    application_id: "app-demo-001",
    applicant_id: "cust-1001",
    requested_amount_usd: 15000,
    loan_purpose: "inventory",
    submission_channel: "portal",
    submitted_at: new Date().toISOString()
  }
};

function prettyDate(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return date.toLocaleString();
}

function isAuthFailure(error: unknown): boolean {
  return error instanceof LedgerApiError && (error.status === 401 || error.status === 403);
}

export default function App() {
  const [authReady, setAuthReady] = useState<boolean>(false);
  const [authUser, setAuthUser] = useState<MeResponse | null>(null);
  const [loginUsername, setLoginUsername] = useState<string>("analyst");
  const [loginPassword, setLoginPassword] = useState<string>("analyst123!");

  const [toolDefinitions, setToolDefinitions] = useState<ToolDefinition[]>([]);
  const [selectedTool, setSelectedTool] = useState<string>("submit_application");
  const [commandPayload, setCommandPayload] = useState<string>(
    JSON.stringify(DEFAULT_COMMAND_PAYLOAD.submit_application, null, 2)
  );
  const [commandResult, setCommandResult] = useState<string>("");

  const [applicationId, setApplicationId] = useState<string>("");
  const [agentId, setAgentId] = useState<string>("credit-agent-demo");

  const [states, setStates] = useState<AppStateCount[]>([]);
  const [applications, setApplications] = useState<ApplicationSummary[]>([]);
  const [application, setApplication] = useState<ApplicationSummary | null>(null);
  const [compliance, setCompliance] = useState<ComplianceView | null>(null);
  const [agentPerformance, setAgentPerformance] = useState<AgentPerformance | null>(null);
  const [health, setHealth] = useState<LedgerHealth | null>(null);
  const [recentEvents, setRecentEvents] = useState<RecentEvent[]>([]);
  const [auditRows, setAuditRows] = useState<AuthAuditRow[]>([]);

  const [busy, setBusy] = useState<boolean>(false);
  const [notice, setNotice] = useState<string>("");
  const [lastRefresh, setLastRefresh] = useState<string>(new Date().toISOString());

  const projectionRows = useMemo(() => {
    if (!health) {
      return [];
    }
    return Object.entries(health.projections);
  }, [health]);

  const canViewAudit = useMemo(() => {
    return authUser?.role === "compliance" || authUser?.role === "admin";
  }, [authUser]);

  const doLogout = useCallback((reason?: string) => {
    clearStoredToken();
    setAuthUser(null);
    setToolDefinitions([]);
    setCommandResult("");
    setAuditRows([]);
    setNotice(reason ?? "Logged out.");
  }, []);

  const refreshGlobal = useCallback(async () => {
    const tasks = [
      fetchApplicationStates(),
      fetchApplications(25),
      fetchLedgerHealth(),
      fetchRecentEvents(20),
      fetchTools()
    ] as const;

    const [nextStates, nextApps, nextHealth, nextEvents, nextTools] = await Promise.all(tasks);

    setStates(nextStates);
    setApplications(nextApps);
    setHealth(nextHealth);
    setRecentEvents(nextEvents);
    setToolDefinitions(nextTools);

    if (nextTools.length > 0 && !nextTools.some((item) => item.name === selectedTool)) {
      setSelectedTool(nextTools[0].name);
      setCommandPayload(JSON.stringify({ application_id: "app-demo-001" }, null, 2));
    }

    if (canViewAudit) {
      const audit = await fetchAuthAudit(40);
      setAuditRows(audit);
    } else {
      setAuditRows([]);
    }

    setLastRefresh(new Date().toISOString());
  }, [canViewAudit, selectedTool]);

  const refreshFocused = useCallback(async () => {
    const tasks: Promise<void>[] = [];

    if (applicationId.trim()) {
      tasks.push(
        (async () => {
          const nextApplication = await fetchApplication(applicationId.trim());
          setApplication(nextApplication);

          // Compliance view is expected to be missing until compliance events exist.
          if (!nextApplication.compliance_status) {
            setCompliance(null);
            return;
          }

          try {
            const nextCompliance = await fetchCompliance(applicationId.trim());
            setCompliance(nextCompliance);
          } catch (error) {
            if (error instanceof LedgerApiError && error.status === 404) {
              setCompliance(null);
              return;
            }
            throw error;
          }
        })()
      );
    }

    if (agentId.trim()) {
      tasks.push(
        (async () => {
          const nextPerformance = await fetchAgentPerformance(agentId.trim());
          setAgentPerformance(nextPerformance);
        })()
      );
    }

    if (tasks.length > 0) {
      await Promise.all(tasks);
      setLastRefresh(new Date().toISOString());
    }
  }, [applicationId, agentId]);

  const loadEverything = useCallback(async () => {
    if (!authUser) {
      return;
    }

    try {
      await refreshGlobal();
      await refreshFocused();
      setNotice("");
    } catch (error) {
      if (isAuthFailure(error)) {
        doLogout("Session expired. Please login again.");
        return;
      }
      if (error instanceof LedgerApiError) {
        setNotice(`${error.message} (${error.errorType ?? "RequestError"})`);
      } else {
        setNotice("Unexpected UI refresh error.");
      }
    }
  }, [authUser, doLogout, refreshFocused, refreshGlobal]);

  useEffect(() => {
    (async () => {
      try {
        const me = await fetchMe();
        setAuthUser(me);
        setNotice("");
      } catch {
        clearStoredToken();
      } finally {
        setAuthReady(true);
      }
    })();
  }, []);

  useEffect(() => {
    if (!authUser) {
      return;
    }
    void loadEverything();
  }, [authUser, loadEverything]);

  useEffect(() => {
    if (!authUser) {
      return;
    }
    const timer = window.setInterval(() => {
      void loadEverything();
    }, 7000);
    return () => window.clearInterval(timer);
  }, [authUser, loadEverything]);

  async function handleLogin(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    try {
      const session = await login(loginUsername.trim(), loginPassword);
      setAuthUser({
        username: session.user.username,
        role: session.user.role,
        issued_at: session.user.issued_at,
        expires_at: session.expires_at,
        allowed_commands: session.allowed_commands
      });
      setNotice(`Welcome ${session.user.username}.`);
    } catch (error) {
      if (error instanceof LedgerApiError) {
        setNotice(error.message);
      } else {
        setNotice("Login failed unexpectedly.");
      }
    } finally {
      setBusy(false);
    }
  }

  async function handleRunDemo() {
    setBusy(true);
    setNotice("Creating demo lifecycle...");
    try {
      const result = await bootstrapDemo();
      const nextApplicationId = String(result.application_id ?? "");
      const nextAgentId = String(result.agent_id ?? "");
      if (nextApplicationId) {
        setApplicationId(nextApplicationId);
      }
      if (nextAgentId) {
        setAgentId(nextAgentId);
      }
      setNotice(`Demo scenario ready: ${nextApplicationId}`);
      await loadEverything();
    } catch (error) {
      if (isAuthFailure(error)) {
        doLogout("Session expired. Please login again.");
        return;
      }
      if (error instanceof LedgerApiError) {
        setNotice(`Demo bootstrap failed: ${error.message}`);
      } else {
        setNotice("Demo bootstrap failed unexpectedly.");
      }
    } finally {
      setBusy(false);
    }
  }

  async function handleRunCommand() {
    setBusy(true);
    setNotice(`Running ${selectedTool}...`);

    try {
      const parsed = JSON.parse(commandPayload) as Record<string, unknown>;
      const result = await runCommand(selectedTool, parsed);
      setCommandResult(JSON.stringify(result, null, 2));

      const fromPayload = String((parsed.application_id as string | undefined) ?? "");
      if (fromPayload) {
        setApplicationId(fromPayload);
      }
      const fromAgent = String((parsed.agent_id as string | undefined) ?? "");
      if (fromAgent) {
        setAgentId(fromAgent);
      }

      setNotice(`${selectedTool} completed.`);
      await loadEverything();
    } catch (error) {
      if (isAuthFailure(error)) {
        doLogout("Session expired. Please login again.");
        return;
      }
      if (error instanceof SyntaxError) {
        setNotice("Invalid JSON in command payload.");
      } else if (error instanceof LedgerApiError) {
        setNotice(
          `${error.message}${error.suggestedAction ? ` | Try: ${error.suggestedAction}` : ""}`
        );
      } else {
        setNotice("Command execution failed unexpectedly.");
      }
    } finally {
      setBusy(false);
    }
  }

  function handleToolChange(nextTool: string) {
    setSelectedTool(nextTool);
    const seeded =
      DEFAULT_COMMAND_PAYLOAD[nextTool as keyof typeof DEFAULT_COMMAND_PAYLOAD] ??
      ({ application_id: applicationId || "app-demo-001" } as Record<string, unknown>);
    setCommandPayload(JSON.stringify(seeded, null, 2));
  }

  if (!authReady) {
    return <div className="auth-shell">Initializing dashboard...</div>;
  }

  if (!authUser) {
    return (
      <div className="auth-shell">
        <form className="auth-card" onSubmit={(event) => void handleLogin(event)}>
          <p className="eyebrow">Ledger Access</p>
          <h1>Sign In</h1>
          <p className="muted">Use a role account to unlock role-scoped tools and data views.</p>
          <label>
            Username
            <input value={loginUsername} onChange={(event) => setLoginUsername(event.target.value)} />
          </label>
          <label>
            Password
            <input
              type="password"
              value={loginPassword}
              onChange={(event) => setLoginPassword(event.target.value)}
            />
          </label>
          <button className="button primary" type="submit" disabled={busy}>
            {busy ? "Signing in..." : "Sign In"}
          </button>
          <p className="muted">Demo users: analyst, compliance, ops, admin</p>
          <p className="notice-inline">{notice || ""}</p>
        </form>
      </div>
    );
  }

  return (
    <div className="page-shell">
      <div className="bg-orb bg-orb-a" />
      <div className="bg-orb bg-orb-b" />

      <header className="topbar panel">
        <div>
          <p className="eyebrow">Ledger Operations Center</p>
          <h1>Agentic Event Store Dashboard</h1>
          <p className="subtitle">
            Command-side writes, projection health, and audit-grade read models in one live view.
          </p>
        </div>
        <div className="topbar-right">
          <div className="status-pill">Last refresh: {prettyDate(lastRefresh)}</div>
          <div className="status-pill">
            {authUser.username} ({authUser.role})
          </div>
          <button className="button" onClick={() => doLogout("Logged out.")}>Log Out</button>
        </div>
      </header>

      <section className="grid grid-2">
        <article className="panel action-panel">
          <h2>Scenario Runner</h2>
          <p className="muted">
            Create a full loan decision lifecycle with one click so stakeholders can inspect real data.
          </p>
          <button className="button primary" onClick={() => void handleRunDemo()} disabled={busy}>
            {busy ? "Working..." : "Bootstrap Demo Flow"}
          </button>
        </article>

        <article className="panel action-panel">
          <h2>Focus Filters</h2>
          <label>
            Application ID
            <input
              value={applicationId}
              onChange={(event) => setApplicationId(event.target.value)}
              placeholder="app-..."
            />
          </label>
          <label>
            Agent ID
            <input
              value={agentId}
              onChange={(event) => setAgentId(event.target.value)}
              placeholder="credit-agent-demo"
            />
          </label>
          <button className="button" onClick={() => void loadEverything()} disabled={busy}>
            Refresh Views
          </button>
        </article>
      </section>

      <section className="grid grid-3">
        <article className="panel">
          <h2>Pipeline States</h2>
          {states.length === 0 ? <p className="muted">No projected applications yet.</p> : null}
          <div className="chip-wrap">
            {states.map((item) => (
              <div className="state-chip" key={item.state}>
                <span>{item.state}</span>
                <strong>{item.count}</strong>
              </div>
            ))}
          </div>
        </article>

        <article className="panel">
          <h2>Projection Lag</h2>
          {projectionRows.length === 0 ? <p className="muted">No lag data yet.</p> : null}
          <div className="lag-list">
            {projectionRows.map(([name, lag]) => (
              <div className="lag-row" key={name}>
                <span>{name}</span>
                <span>{lag.events_behind} events</span>
                <span className={`badge ${lag.status.toLowerCase()}`}>{lag.status}</span>
              </div>
            ))}
          </div>
        </article>

        <article className="panel">
          <h2>Tracked Applications</h2>
          <div className="table">
            {applications.slice(0, 8).map((item) => (
              <button
                key={item.application_id}
                className="table-row"
                onClick={() => setApplicationId(item.application_id)}
              >
                <span>{item.application_id}</span>
                <span>{item.current_state}</span>
              </button>
            ))}
          </div>
        </article>
      </section>

      <section className="grid grid-2">
        <article className="panel">
          <h2>Command Console</h2>
          {toolDefinitions.length === 0 ? (
            <p className="muted">No commands available for your role.</p>
          ) : (
            <>
              <label>
                Tool
                <select
                  value={selectedTool}
                  onChange={(event) => handleToolChange(event.target.value)}
                >
                  {toolDefinitions.map((tool) => (
                    <option value={tool.name} key={tool.name}>
                      {tool.name}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                Arguments JSON
                <textarea
                  value={commandPayload}
                  onChange={(event) => setCommandPayload(event.target.value)}
                  rows={12}
                />
              </label>
              <button className="button" onClick={() => void handleRunCommand()} disabled={busy}>
                Run Command
              </button>
            </>
          )}
          <pre className="result-box">{commandResult || "Command result will appear here."}</pre>
        </article>

        <article className="panel">
          <h2>Application Snapshot</h2>
          {!application ? <p className="muted">Set an Application ID or run the demo flow.</p> : null}
          {application ? (
            <dl className="key-values">
              <div>
                <dt>Application</dt>
                <dd>{application.application_id}</dd>
              </div>
              <div>
                <dt>State</dt>
                <dd>{application.current_state}</dd>
              </div>
              <div>
                <dt>Compliance</dt>
                <dd>{application.compliance_status ?? "-"}</dd>
              </div>
              <div>
                <dt>Requested</dt>
                <dd>{application.requested_amount_usd ?? "-"}</dd>
              </div>
              <div>
                <dt>Approved</dt>
                <dd>{application.approved_amount_usd ?? "-"}</dd>
              </div>
              <div>
                <dt>Updated</dt>
                <dd>{prettyDate(application.updated_at)}</dd>
              </div>
            </dl>
          ) : null}

          <h3>Compliance Timeline</h3>
          <div className="timeline">
            {compliance?.timeline?.slice(-8).map((entry) => (
              <div className="timeline-row" key={`${entry.global_position}-${entry.event_type}`}>
                <span>{entry.event_type}</span>
                <span>{entry.compliance_status}</span>
                <span>{prettyDate(entry.recorded_at)}</span>
              </div>
            ))}
          </div>
        </article>
      </section>

      <section className="grid grid-2">
        <article className="panel">
          <h2>Agent Performance</h2>
          {!agentPerformance || agentPerformance.models.length === 0 ? (
            <p className="muted">No projections for this agent yet.</p>
          ) : (
            <div className="table">
              {agentPerformance.models.map((model) => (
                <div className="table-row static" key={`${model.agent_id}-${model.model_version}`}>
                  <span>{model.model_version}</span>
                  <span>sessions {model.sessions_started}</span>
                  <span>avg conf {model.avg_confidence_score.toFixed(2)}</span>
                </div>
              ))}
            </div>
          )}
        </article>

        <article className="panel">
          <h2>Recent Events</h2>
          <div className="timeline">
            {recentEvents.slice(0, 12).map((event) => (
              <div className="timeline-row" key={event.event_id}>
                <span>{event.event_type}</span>
                <span>{event.stream_id}</span>
                <span>#{event.global_position}</span>
              </div>
            ))}
          </div>
        </article>
      </section>

      {canViewAudit ? (
        <section className="grid grid-1">
          <article className="panel">
            <h2>Auth Audit Log</h2>
            <div className="timeline">
              {auditRows.slice(0, 30).map((row) => (
                <div className="timeline-row" key={row.audit_id}>
                  <span>{row.action}</span>
                  <span>{row.username ?? "anonymous"}</span>
                  <span>{row.success ? "success" : "failed"}</span>
                </div>
              ))}
            </div>
          </article>
        </section>
      ) : null}

      <footer className="notice panel">{notice || "Ready."}</footer>
    </div>
  );
}
