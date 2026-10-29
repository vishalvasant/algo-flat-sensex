import { useCallback, useEffect, useState } from "react";
import { Bell, KeyRound, RefreshCw, Save, Shield } from "lucide-react";
import {
  fetchFlattradeCredentials,
  fetchHealth,
  fetchTelegramStatus,
  linkTelegram,
  reauthenticate,
  saveFlattradeCredentials,
  testTelegram,
  type TelegramStatus,
} from "../api/client";
import type { FlattradeCredentialsStatus } from "../types";
import { AppPageShell } from "../components/AppPageShell";
import { StatusBadge } from "../components/StatusBadge";

const EMPTY_FORM = {
  user_id: "",
  api_key: "",
  api_secret: "",
  password: "",
  totp_secret: "",
  redirect_url: "http://127.0.0.1:8000/callback",
  access_token: "",
  analytic_token: "",
};

export function FlattradeSettingsPage() {
  const [status, setStatus] = useState<FlattradeCredentialsStatus | null>(null);
  const [form, setForm] = useState(EMPTY_FORM);
  const [busy, setBusy] = useState(false);
  const [reauthBusy, setReauthBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [sessionValid, setSessionValid] = useState<boolean | null>(null);
  const [telegram, setTelegram] = useState<TelegramStatus | null>(null);
  const [telegramBusy, setTelegramBusy] = useState(false);
  const [provider, setProvider] = useState<"upstox" | "flattrade">("upstox");

  const load = useCallback(() => {
    setBusy(true);
    Promise.all([fetchFlattradeCredentials(), fetchHealth(), fetchTelegramStatus()])
      .then(([creds, health, tg]) => {
        setStatus(creds);
        setTelegram(tg);
        setForm((prev) => ({
          ...prev,
          user_id: creds.user_id ?? "",
          redirect_url: creds.redirect_url || EMPTY_FORM.redirect_url,
        }));
        setSessionValid(
          (health.broker_session ?? health.flattrade_session)?.valid ?? false,
        );
        const credsProvider = String(
          health.broker_credentials?.provider ?? "upstox",
        ).toLowerCase();
        setProvider(credsProvider === "flattrade" ? "flattrade" : "upstox");
        setError(null);
      })
      .catch((e) => setError(String(e)))
      .finally(() => setBusy(false));
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const onChange = (field: keyof typeof EMPTY_FORM, value: string) => {
    setForm((prev) => ({ ...prev, [field]: value }));
    setMessage(null);
  };

  const onSave = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    setMessage(null);
    try {
      const payload: Record<string, string> = {};
      if (form.user_id.trim()) payload.user_id = form.user_id.trim();
      if (form.api_key.trim()) payload.api_key = form.api_key.trim();
      if (form.api_secret.trim()) payload.api_secret = form.api_secret.trim();
      if (form.password.trim()) payload.password = form.password.trim();
      if (form.totp_secret.trim()) payload.totp_secret = form.totp_secret.trim();
      if (form.redirect_url.trim()) payload.redirect_url = form.redirect_url.trim();
      if (form.access_token.trim()) payload.access_token = form.access_token.trim();
      if (form.analytic_token.trim()) payload.analytic_token = form.analytic_token.trim();

      const saved = await saveFlattradeCredentials(payload);
      setStatus(saved);
      setForm((prev) => ({
        ...prev,
        api_key: "",
        api_secret: "",
        password: "",
        totp_secret: "",
        access_token: "",
        analytic_token: "",
        user_id: saved.user_id ?? prev.user_id,
        redirect_url: saved.redirect_url,
      }));
      setMessage(
        provider === "upstox"
          ? "Upstox credentials saved. Click Re-authenticate to connect the engine."
          : "Credentials saved to database. Use Re-authenticate to refresh the broker session.",
      );
    } catch (err) {
      setError(String(err));
    } finally {
      setBusy(false);
    }
  };

  const onReauth = async () => {
    setReauthBusy(true);
    setError(null);
    setMessage(null);
    try {
      await reauthenticate(true);
      setMessage("Broker session refreshed.");
      const health = await fetchHealth();
      setSessionValid(
        (health.broker_session ?? health.flattrade_session)?.valid ?? false,
      );
    } catch (err) {
      setError(String(err));
    } finally {
      setReauthBusy(false);
    }
  };

  const onTelegramLink = async () => {
    setTelegramBusy(true);
    setError(null);
    setMessage(null);
    try {
      const result = await linkTelegram();
      setTelegram(result);
      setMessage(
        result.chat_id
          ? `Telegram linked (chat ${result.chat_id}).`
          : "No chat found — open your bot in Telegram and tap Start, then try again.",
      );
    } catch (err) {
      setError(String(err));
    } finally {
      setTelegramBusy(false);
    }
  };

  const onTelegramTest = async () => {
    setTelegramBusy(true);
    setError(null);
    setMessage(null);
    try {
      const result = await testTelegram();
      setTelegram(result);
      if (result.ok) {
        setMessage("Test alert sent to Telegram.");
      } else {
        setError(result.error ?? result.hint ?? "Telegram test failed");
      }
    } catch (err) {
      setError(String(err));
    } finally {
      setTelegramBusy(false);
    }
  };

  const sourceLabel =
    status?.source === "database"
      ? "Database"
      : status?.source === "environment"
        ? "Environment (.env)"
        : status?.source === "mixed"
          ? "Database + .env"
          : "Not configured";

  return (
    <AppPageShell
      title={provider === "flattrade" ? "Flattrade" : "Upstox"}
      icon={KeyRound}
      description={
        provider === "flattrade"
          ? "Flattrade API credentials (TOTP auto-login). Session token is cached in DB + file until ~05:00 IST."
          : "Broker API credentials and Telegram alerts for the trading engine"
      }
    >
      <div className="logs-page-full settings-page">
        <section className="cockpit-panel logs-stats-panel">
          <header className="cockpit-panel-head logs-stats-head">
            <Shield size={14} strokeWidth={2} />
            <h3>Connection status</h3>
            <button
              type="button"
              className="btn btn-ghost btn-sm logs-refresh-btn"
              onClick={load}
              disabled={busy}
            >
              <RefreshCw size={13} className={busy ? "spin" : undefined} />
              {busy ? "Loading…" : "Refresh"}
            </button>
          </header>
          <div className="cockpit-command-metrics logs-command-metrics settings-metrics">
            <div className="cmd-metric">
              <span>Credential source</span>
              <strong>{sourceLabel}</strong>
            </div>
            <div className="cmd-metric">
              <span>API configured</span>
              <strong>{status?.has_api_credentials ? "Yes" : "No"}</strong>
            </div>
            <div className="cmd-metric">
              <span>{provider === "flattrade" ? "Auto login (TOTP)" : "Access token"}</span>
              <strong>
                {provider === "flattrade"
                  ? status?.has_auto_login
                    ? "Ready"
                    : "Incomplete"
                  : status?.has_access_token
                    ? "Valid"
                    : status?.access_token_set
                      ? "Expired"
                      : "Not set"}
              </strong>
            </div>
            {provider === "flattrade" ? (
              <div className="cmd-metric">
                <span>Cached token</span>
                <strong>
                  {status?.has_access_token
                    ? "Valid"
                    : status?.access_token_set
                      ? "Expired"
                      : "None"}
                </strong>
              </div>
            ) : (
              <div className="cmd-metric">
                <span>Analytic token</span>
                <strong>{status?.analytic_token_set ? "Set" : "Not set"}</strong>
              </div>
            )}
            <div className="cmd-metric">
              <span>Session</span>
              <strong
                className={
                  sessionValid === null
                    ? undefined
                    : sessionValid
                      ? "positive"
                      : "negative"
                }
              >
                {sessionValid === null ? "—" : sessionValid ? "Valid" : "Expired / missing"}
              </strong>
            </div>
          </div>
          {status?.api_key_masked ? (
            <p className="settings-hint mono">
              Current API key: <span>{status.api_key_masked}</span>
            </p>
          ) : null}
        </section>

        {error ? <div className="error-banner">{error}</div> : null}
        {message ? <div className="cockpit-flash-banner settings-success-banner">{message}</div> : null}

        <section className="cockpit-panel settings-form-panel orderbook-table-panel">
          <header className="cockpit-panel-head">
            <KeyRound size={14} strokeWidth={2} />
            <h3>Credentials</h3>
            <StatusBadge
              label={status?.has_api_credentials ? "Configured" : "Required"}
              severity={status?.has_api_credentials ? "success" : "warning"}
            />
          </header>

          <form className="settings-form" onSubmit={onSave}>
            {provider === "flattrade" ? (
            <label className="settings-field">
              <span>User ID</span>
              <input
                type="text"
                className="mono"
                value={form.user_id}
                onChange={(e) => onChange("user_id", e.target.value)}
                placeholder="e.g. FZ49363"
                autoComplete="off"
              />
            </label>
            ) : null}

            <label className="settings-field">
              <span>API key</span>
              <input
                type="password"
                className="mono"
                value={form.api_key}
                onChange={(e) => onChange("api_key", e.target.value)}
                placeholder={
                  status?.api_key_masked
                    ? "Leave blank to keep current"
                    : provider === "flattrade"
                      ? "From Flattrade Wall → Pi"
                      : "From Upstox developer app"
                }
                autoComplete="off"
              />
            </label>

            <label className="settings-field">
              <span>API secret</span>
              <input
                type="password"
                className="mono"
                value={form.api_secret}
                onChange={(e) => onChange("api_secret", e.target.value)}
                placeholder={status?.api_secret_set ? "Leave blank to keep current" : "API secret"}
                autoComplete="off"
              />
            </label>

            {provider === "flattrade" ? (
            <>
            <label className="settings-field">
              <span>Password</span>
              <input
                type="password"
                className="mono"
                value={form.password}
                onChange={(e) => onChange("password", e.target.value)}
                placeholder={
                  status?.password_set ? "Leave blank to keep current" : "Flattrade login password"
                }
                autoComplete="off"
              />
            </label>

            <label className="settings-field">
              <span>TOTP secret</span>
              <input
                type="password"
                className="mono"
                value={form.totp_secret}
                onChange={(e) => onChange("totp_secret", e.target.value)}
                placeholder={
                  status?.totp_secret_set
                    ? "Leave blank to keep current"
                    : "Profile → Security → TOTP secret"
                }
                autoComplete="off"
              />
            </label>
            </>
            ) : (
            <>
            <label className="settings-field">
              <span>Access token</span>
              <input
                type="password"
                className="mono"
                value={form.access_token}
                onChange={(e) => onChange("access_token", e.target.value)}
                placeholder={
                  status?.access_token_set
                    ? "Leave blank to keep current — paste AccessToken= from UpStox.txt"
                    : "AccessToken= from UpStox.txt (expires daily ~03:30 IST)"
                }
                autoComplete="off"
              />
            </label>

            <label className="settings-field">
              <span>Analytic token</span>
              <input
                type="password"
                className="mono"
                value={form.analytic_token}
                onChange={(e) => onChange("analytic_token", e.target.value)}
                placeholder={
                  status?.analytic_token_set
                    ? "Leave blank to keep current — paste Analytic-Token= from UpStox.txt"
                    : "Analytic-Token= from UpStox.txt (historical / Plus APIs)"
                }
                autoComplete="off"
              />
            </label>

            <p className="settings-form-note">
              Paste values from your <span className="mono">UpStox.txt</span> file:{" "}
              <span className="mono">API</span>, <span className="mono">Secrate</span>,{" "}
              <span className="mono">AccessToken</span>, and{" "}
              <span className="mono">Analytic-Token</span>. Access token expires daily — update
              it here each morning, then click Re-authenticate. OAuth via{" "}
              <span className="mono">scripts/upstox_login.py</span> still works as an alternative.
            </p>
            </>
            )}

            <label className="settings-field settings-field--wide">
              <span>OAuth redirect URL</span>
              <input
                type="url"
                className="mono"
                value={form.redirect_url}
                onChange={(e) => onChange("redirect_url", e.target.value)}
                placeholder="http://127.0.0.1:8000/callback"
                autoComplete="off"
              />
            </label>

            <p className="settings-form-note">
              Secrets are stored in Postgres. Leave password fields empty when updating other fields
              to keep existing values. Re-authenticate uses the saved row — one click, no re-entry.
            </p>

            <div className="settings-form-actions">
              <button type="submit" className="btn btn-primary btn-sm" disabled={busy}>
                <Save size={13} />
                {busy ? "Saving…" : "Save credentials"}
              </button>
              <button
                type="button"
                className="btn btn-ghost btn-sm"
                onClick={onReauth}
                disabled={
                  reauthBusy ||
                  !(provider === "flattrade"
                    ? status?.has_auto_login
                    : status?.has_access_token ||
                      status?.access_token_set ||
                      status?.has_api_credentials)
                }
              >
                <RefreshCw size={13} className={reauthBusy ? "spin" : undefined} />
                {reauthBusy ? "Connecting…" : "Re-authenticate"}
              </button>
            </div>
          </form>
        </section>

        <section className="cockpit-panel settings-form-panel orderbook-table-panel">
          <header className="cockpit-panel-head">
            <Bell size={14} strokeWidth={2} />
            <h3>Telegram alerts</h3>
            <StatusBadge
              label={telegram?.ready ? "Ready" : telegram?.configured ? "Not linked" : "Not configured"}
              severity={telegram?.ready ? "success" : telegram?.configured ? "warning" : "neutral"}
            />
          </header>
          <div className="cockpit-command-metrics logs-command-metrics settings-metrics">
            <div className="cmd-metric">
              <span>Bot token</span>
              <strong>{telegram?.configured ? "Configured" : "Missing"}</strong>
            </div>
            <div className="cmd-metric">
              <span>Chat linked</span>
              <strong>{telegram?.linked ? "Yes" : "No"}</strong>
            </div>
            <div className="cmd-metric">
              <span>Alerts enabled</span>
              <strong>{telegram?.enabled ? "Yes" : "No"}</strong>
            </div>
          </div>
          <p className="settings-form-note">
            Set <span className="mono">TELEGRAM_BOT_TOKEN</span> and{" "}
            <span className="mono">TELEGRAM_CHAT_ID</span> in server <span className="mono">.env</span>,
            or tap Link after starting a chat with your bot. Trade and risk alerts are sent in live mode.
          </p>
          <div className="settings-form-actions">
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              onClick={onTelegramLink}
              disabled={telegramBusy || !telegram?.configured}
            >
              <RefreshCw size={13} className={telegramBusy ? "spin" : undefined} />
              {telegramBusy ? "Working…" : "Link chat"}
            </button>
            <button
              type="button"
              className="btn btn-primary btn-sm"
              onClick={onTelegramTest}
              disabled={telegramBusy || !telegram?.configured}
            >
              <Bell size={13} />
              Send test alert
            </button>
          </div>
        </section>
      </div>
    </AppPageShell>
  );
}
