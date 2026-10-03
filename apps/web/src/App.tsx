import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import { ApiError, checkHealth, getSession, signIn, signOut } from "./api";
import type { ApiErrorKind, Session } from "./api";
import { copy, LOCALE_KEY, readLocale } from "./i18n";
import type { Locale, Text } from "./i18n";

type IconName = "arrow" | "lock" | "eye" | "eyeOff" | "grid" | "wallet" | "receipt" | "chart" | "logout" | "refresh" | "check" | "server" | "database" | "globe";
const paths: Record<IconName, string[]> = {
  arrow: ["M5 12h14m-5-5 5 5-5 5"],
  lock: ["M7 10V7a5 5 0 0 1 10 0v3", "M5 10h14v11H5z", "M12 14v3"],
  eye: ["M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12Z", "M15 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0Z"],
  eyeOff: ["m3 3 18 18", "M10 5.2A9 9 0 0 1 12 5c6 0 10 7 10 7a20 20 0 0 1-3 3.8", "M6.5 6.5C3.8 8.7 2 12 2 12s4 7 10 7a10 10 0 0 0 5.5-1.5", "M9.9 9.9a3 3 0 0 0 4.2 4.2"],
  grid: ["M3 3h7v7H3zM14 3h7v7h-7zM3 14h7v7H3zM14 14h7v7h-7z"],
  wallet: ["M20 8V5H5a2 2 0 0 0 0 4h16v11H5a2 2 0 0 1-2-2V7", "M21 12h-5v4h5"],
  receipt: ["m5 3 3 2 4-2 4 2 3-2v18l-3-2-4 2-4-2-3 2V3Z", "M9 9h6M9 13h6"],
  chart: ["M4 4v16h16", "M8 15v-4M13 15V7M18 15v-6"],
  logout: ["M9 4H4v16h5", "M8 12h12m-4-4 4 4-4 4"],
  refresh: ["M20 7v5h-5", "M4 17v-5h5", "M5.1 8a8 8 0 0 1 13.3-3L20 7M4 17l1.6 2A8 8 0 0 0 19 16"],
  check: ["m5 12 4 4L19 6"],
  server: ["M3 4h18v6H3zM3 14h18v6H3z", "M7 7h.01M7 17h.01M11 7h6M11 17h6"],
  database: ["M20 6c0 2-3.6 4-8 4s-8-2-8-4 3.6-4 8-4 8 2 8 4Z", "M4 6v12c0 2 3.6 4 8 4s8-2 8-4V6", "M4 12c0 2 3.6 4 8 4s8-2 8-4"],
  globe: ["M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z", "M3 12h18M12 3c4 5 4 13 0 18-4-5-4-13 0-18Z"],
};

function Icon({ name, className = "" }: { name: IconName; className?: string }) {
  return <svg className={`icon ${className}`} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{paths[name].map((path, index) => <path d={path} key={index} />)}</svg>;
}

function Brand() {
  return <a className="brand" href="/" aria-label="Coinpup"><img src="/coinpup.svg" alt="" width="42" height="42" /><span>Coinpup<span className="brand-dot">.</span></span></a>;
}

function LanguageSwitch({ locale, onChange, t }: { locale: Locale; onChange: (locale: Locale) => void; t: Text }) {
  return <div className="language-switch" role="group" aria-label={t.language}><button type="button" lang="zh-CN" aria-pressed={locale === "zh"} onClick={() => onChange("zh")}>中文</button><button type="button" lang="en" aria-label="English" aria-pressed={locale === "en"} onClick={() => onChange("en")}>EN</button></div>;
}

function Illustration() {
  return <svg className="hero-illustration" viewBox="0 0 510 240" fill="none" aria-hidden="true">
    <circle cx="261" cy="132" r="97" fill="#e5e9d8" />
    <circle cx="261" cy="132" r="121" stroke="#dfe3d4" strokeDasharray="3 9" />
    <g transform="rotate(-10 185 122)"><rect x="102" y="50" width="180" height="132" rx="19" fill="#d1dfb6" stroke="#bccda0" /><rect x="119" y="70" width="33" height="33" rx="10" fill="#f2f5e9" /><path d="M128 87h15m-11-5-4 5 4 5m7-10 4 5-4 5" stroke="#66815a" strokeWidth="2" strokeLinecap="round" /><path d="M121 127h80M121 141h120M121 155h49" stroke="#afc296" strokeWidth="6" strokeLinecap="round" /></g>
    <g transform="rotate(8 315 146)"><rect x="221" y="84" width="185" height="131" rx="19" fill="#fffefa" stroke="#e0e2d7" /><rect x="240" y="103" width="33" height="33" rx="10" fill="#f0f2e9" /><path d="M249 114h15v13h-15zM254 110h6v4" stroke="#718168" strokeWidth="1.8" strokeLinejoin="round" /><path d="M241 158h76M241 173h115M241 188h46" stroke="#e4e8dc" strokeWidth="6" strokeLinecap="round" /></g>
    <circle cx="373" cy="56" r="25" fill="#284c3d" /><path d="m362 56 7 7 14-15" stroke="#e7f3c6" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round" />
    <path d="M83 107h12m-6-6v12M422 165h12m-6-6v12" stroke="#829774" strokeWidth="2" strokeLinecap="round" />
    <circle cx="128" cy="207" r="4" fill="#c9a979" />
  </svg>;
}

function feedback(kind: ApiErrorKind, t: Text): string {
  if (kind === "unauthorized") return t.credentialsError;
  if (kind === "rate-limited") return t.rateError;
  if (kind === "network") return t.networkError;
  return t.serviceError;
}

function LoginForm({ t, onSuccess }: { t: Text; onSuccess: (session: Session) => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [visible, setVisible] = useState(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<ApiErrorKind | null>(null);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (pending) return;
    setPending(true);
    setError(null);
    try {
      onSuccess(await signIn(username.trim(), password));
    } catch (problem) {
      setError(problem instanceof ApiError ? problem.kind : "server");
    } finally {
      setPassword("");
      setPending(false);
    }
  }

  return <section className="login-card" aria-labelledby="login-title">
    <div className="card-emblem"><Icon name="lock" /></div>
    <h2 id="login-title">{t.welcome}</h2>
    <p className="muted login-intro">{t.loginIntro}</p>
    <form onSubmit={submit} aria-busy={pending}>
      <div className="field"><label htmlFor="username">{t.username}</label><input id="username" name="username" autoComplete="username" autoCapitalize="none" spellCheck={false} required maxLength={64} placeholder={t.usernamePlaceholder} value={username} disabled={pending} onChange={(event) => { setUsername(event.target.value); setError(null); }} /></div>
      <div className="field"><label htmlFor="password">{t.password}</label><div className="password-input"><input id="password" name="password" type={visible ? "text" : "password"} autoComplete="current-password" required maxLength={128} placeholder={t.passwordPlaceholder} value={password} disabled={pending} onChange={(event) => { setPassword(event.target.value); setError(null); }} /><button type="button" className="password-toggle" aria-label={visible ? t.hidePassword : t.showPassword} aria-pressed={visible} disabled={pending} onClick={() => setVisible(!visible)}><Icon name={visible ? "eyeOff" : "eye"} /></button></div></div>
      {error && <p className="form-error" role="alert">{feedback(error, t)}</p>}
      <button className="primary-button" type="submit" disabled={pending}>{pending ? <><span className="spinner" />{t.signingIn}</> : <>{t.signIn}<Icon name="arrow" /></>}</button>
    </form>
    <p className="login-hint"><Icon name="lock" />{t.adminHint}</p>
  </section>;
}

type AuthState = { status: "checking" } | { status: "anonymous" } | { status: "unavailable"; reason: ApiErrorKind } | { status: "authenticated"; session: Session };
type HealthState = { live: boolean | null; ready: boolean | null; checkedAt: Date | null; loading: boolean };

function Workspace({ session, locale, t }: { session: Session; locale: Locale; t: Text }) {
  const [health, setHealth] = useState<HealthState>({ live: null, ready: null, checkedAt: null, loading: true });
  const [refreshKey, setRefreshKey] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    setHealth((previous) => ({ ...previous, loading: true }));
    void Promise.allSettled([checkHealth("live", controller.signal), checkHealth("ready", controller.signal)]).then(([live, ready]) => {
      if (controller.signal.aborted) return;
      setHealth({ live: live.status === "fulfilled" && live.value, ready: ready.status === "fulfilled" && ready.value, checkedAt: new Date(), loading: false });
    });
    return () => controller.abort();
  }, [refreshKey]);

  const services = [
    { key: "live", title: t.apiTitle, description: t.apiDescription, icon: "server", value: health.live },
    { key: "ready", title: t.databaseTitle, description: t.databaseDescription, icon: "database", value: health.ready },
  ] as const;

  return <div className="workspace-layout">
    <aside className="sidebar" aria-label={t.workspace}>
      <div className="profile"><div className="avatar">{session.user.username.charAt(0).toUpperCase()}</div><div><strong>{session.user.username}</strong><span>{t.admin}</span></div><span className="profile-dot" /></div>
      <p className="nav-caption">{t.workspace}</p>
      <div className="nav-current"><Icon name="grid" />{t.overview}</div>
      <div className="nav-planned"><Icon name="wallet" /><span>{t.accounts}</span></div>
      <div className="nav-planned"><Icon name="chart" /><span>{t.transactions}</span></div>
      <div className="nav-planned"><Icon name="receipt" /><span>{t.documents}</span></div>
      <span className="nav-note">{t.planned}</span>
      <div className="sidebar-bottom"><span className="tiny-dot" />{t.foundation}</div>
    </aside>
    <main id="main" className="workspace-main">
      <div className="page-heading"><div><p className="eyebrow">{t.hello}<span data-testid="current-username">{session.user.username}</span></p><h1>{t.dashboardTitle}</h1><p className="muted">{t.dashboardDescription}</p></div><span className="preview-badge">{t.preview}</span></div>
      <section className="session-card" aria-labelledby="session-title"><div className="session-icon"><Icon name="check" /></div><div><span className="section-kicker">{t.availableNow}</span><h2 id="session-title">{t.sessionTitle}</h2><p className="muted">{t.sessionDescription}</p></div><span className="status-pill good"><span />{t.signedIn}</span></section>
      <section className="service-section" aria-labelledby="service-title"><div className="section-heading"><div><h2 id="service-title">{t.serviceTitle}</h2><p className="muted">{t.serviceDescription}</p></div><button className="text-button" type="button" disabled={health.loading} onClick={() => setRefreshKey((key) => key + 1)}><Icon name="refresh" className={health.loading ? "rotating" : ""} />{health.loading ? t.refreshing : t.refresh}</button></div><div className="service-grid">{services.map((service) => <article className="service-card" key={service.key}><div className="service-card-top"><span className="service-icon"><Icon name={service.icon} /></span><span data-testid={`${service.key}-status`} className={`status-pill ${health.loading ? "neutral" : service.value ? "good" : "warning"}`} role="status"><span />{health.loading ? t.checking : service.value ? t.available : t.unavailable}</span></div><h3>{service.title}</h3><p className="muted">{service.description}</p></article>)}</div>{health.checkedAt && <p className="checked-at">{t.checkedAt} · {new Intl.DateTimeFormat(locale === "zh" ? "zh-CN" : "en", { hour: "2-digit", minute: "2-digit", second: "2-digit" }).format(health.checkedAt)}</p>}</section>
      <section className="roadmap-card" aria-labelledby="next-title"><div className="roadmap-copy"><span className="section-kicker">{t.notYet}</span><h2 id="next-title">{t.nextTitle}</h2><p>{t.nextDescription}</p><div className="roadmap-mark" aria-hidden="true"><Icon name="receipt" /><span>+</span></div></div><ol className="roadmap-list">{[t.roadmapOne, t.roadmapTwo, t.roadmapThree, t.roadmapFour].map((title, index) => <li key={title}><span className="step-number">0{index + 1}</span><span>{title}</span><span className="planned-label">{t.planned}</span></li>)}</ol></section>
      <footer className="workspace-footer"><span>Coinpup · {t.footer}</span><span>{t.foundation}</span></footer>
    </main>
  </div>;
}

export function App() {
  const [locale, setLocale] = useState<Locale>(readLocale);
  const [auth, setAuth] = useState<AuthState>({ status: "checking" });
  const [retryKey, setRetryKey] = useState(0);
  const [loggingOut, setLoggingOut] = useState(false);
  const [logoutFailed, setLogoutFailed] = useState(false);
  const t = copy[locale];

  useEffect(() => {
    document.documentElement.lang = locale === "zh" ? "zh-CN" : "en";
    try { localStorage.setItem(LOCALE_KEY, locale); } catch { /* Language still works if storage is unavailable. */ }
  }, [locale]);

  useEffect(() => {
    const controller = new AbortController();
    setAuth({ status: "checking" });
    void getSession(controller.signal).then((session) => {
      if (!controller.signal.aborted) setAuth({ status: "authenticated", session });
    }).catch((problem: unknown) => {
      if (controller.signal.aborted) return;
      if (problem instanceof ApiError && problem.kind === "unauthorized") setAuth({ status: "anonymous" });
      else setAuth({ status: "unavailable", reason: problem instanceof ApiError ? problem.kind : "server" });
    });
    return () => controller.abort();
  }, [retryKey]);

  useEffect(() => {
    if (auth.status === "checking" || auth.status === "unavailable") return;
    const path = auth.status === "authenticated" ? "/" : "/login";
    if (window.location.pathname !== path) window.history.replaceState(null, "", path);
    document.title = auth.status === "authenticated" ? `${t.overview} · Coinpup` : `${t.welcome} · Coinpup`;
  }, [auth.status, t.overview, t.welcome]);

  async function logout() {
    if (auth.status !== "authenticated" || loggingOut) return;
    setLoggingOut(true);
    setLogoutFailed(false);
    try {
      await signOut(auth.session.csrf_token);
      setAuth({ status: "anonymous" });
    } catch (problem) {
      if (problem instanceof ApiError && problem.kind === "unauthorized") setAuth({ status: "anonymous" });
      else setLogoutFailed(true);
    } finally {
      setLoggingOut(false);
    }
  }

  return <div className={`app ${auth.status === "authenticated" ? "app-workspace" : "app-entry"}`}>
    <a className="skip-link" href="#main">{t.skip}</a>
    <header className="topbar"><Brand /><div className="topbar-actions"><LanguageSwitch locale={locale} onChange={setLocale} t={t} />{auth.status === "authenticated" && <button className="logout-button" type="button" aria-label={loggingOut ? t.signingOut : t.signOut} onClick={() => void logout()} disabled={loggingOut}><Icon name="logout" /><span>{loggingOut ? t.signingOut : t.signOut}</span></button>}</div></header>
    {logoutFailed && <div className="logout-error" role="alert">{t.logoutError}</div>}
    {auth.status === "authenticated" ? <Workspace session={auth.session} locale={locale} t={t} /> : <>
      <main id="main" className="entry-main"><section className="entry-story"><span className="preview-badge"><span className="tiny-dot" />{t.preview}</span><p className="eyebrow">{t.privateSpace}</p><h1>{t.heroTitle}<br /><span>{t.heroAccent}</span></h1><p className="hero-description">{t.heroDescription}</p><Illustration /><div className="hero-features"><div><Icon name="wallet" /><div><strong>{t.separateBooks}</strong><p>{t.separateDescription}</p></div></div><div><Icon name="globe" /><div><strong>{t.allTogether}</strong><p>{t.allDescription}</p></div></div></div><p className="story-planned">{t.planned}</p></section>
      <div className="entry-form-area">{auth.status === "anonymous" ? <LoginForm t={t} onSuccess={(session) => { setLogoutFailed(false); setAuth({ status: "authenticated", session }); }} /> : <section className="login-card connection-card" aria-live="polite"><div className="card-emblem">{auth.status === "checking" ? <span className="spinner" /> : <Icon name="server" />}</div><h2>{auth.status === "checking" ? t.checkingSession : t.connectionTitle}</h2><p className="muted">{auth.status === "checking" ? t.checkingDescription : feedback(auth.reason, t)}</p>{auth.status === "unavailable" && <button className="primary-button" type="button" onClick={() => setRetryKey((key) => key + 1)}>{t.retry}<Icon name="refresh" /></button>}</section>}<p className="entry-scope">{t.currentScope}</p></div></main>
      <footer className="entry-footer"><span>© {new Date().getFullYear()} Coinpup</span><span>{t.footer}</span></footer>
    </>}
  </div>;
}
