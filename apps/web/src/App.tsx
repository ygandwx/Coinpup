import { Icon } from "./Icon";
import { BrandMark } from "./BrandMark";
import { useEffect, useLayoutEffect, useState } from "react";
import type { FormEvent } from "react";
import { ApiError, getSession, signIn, signOut } from "./api";
import type { ApiErrorKind, Session } from "./api";
import { copy, LOCALE_KEY, readLocale } from "./i18n";
import type { Locale, Text } from "./i18n";
import { BusinessWorkspace } from "./Workspace";
import { PendingCommandController } from "./pending-command";
import { PendingUploadController } from "./pending-upload";
import { PendingConfirmationController } from "./pending-confirmations";
import { OcrJobIntents } from "./ocr-job-intents";
import { PendingPeriodController } from "./pending-period";
import { PendingMasterDataController } from "./pending-business";
import { PendingBusinessDraftController } from "./pending-business-drafts";
import { PendingRecurringController } from "./pending-recurring";
import { PendingBusinessConfirmationController } from "./pending-business-confirmation";
import { PendingBusinessCreditController } from "./pending-business-credit";
import { PendingInvoiceController } from "./pending-invoice";
import { PendingInvoiceProfiles } from "./pending-invoice-profiles";
import { PendingInterledger } from "./pending-interledger";
import { PendingPrice } from "./pending-price";
import { PendingBusinessSettlementController } from "./pending-business-settlement";
import { PendingReminderController } from "./pending-reminder";
import { PendingReminderEmail } from "./pending-reminder-email";
import { OcrCopyController } from "./ocr-copy";

function Brand({ linked = true }: { linked?: boolean }) {
    const content = (
        <>
            <BrandMark />
            <span>
                Coinpup<span className="brand-dot">.</span>
            </span>
        </>
    );
    return linked ? (
        <a className="brand" href="/" aria-label="Coinpup">
            {content}
        </a>
    ) : (
        <span className="brand">{content}</span>
    );
}

function LanguageSwitch({
    locale,
    onChange,
    t,
}: {
    locale: Locale;
    onChange: (locale: Locale) => void;
    t: Text;
}) {
    return (
        <div className="language-switch" role="group" aria-label={t.language}>
            <button
                type="button"
                lang="zh-CN"
                aria-pressed={locale === "zh"}
                onClick={() => onChange("zh")}
            >
                中文
            </button>
            <button
                type="button"
                lang="en"
                aria-label="English"
                aria-pressed={locale === "en"}
                onClick={() => onChange("en")}
            >
                EN
            </button>
        </div>
    );
}

const THEME_KEY = "coinpup.theme";
type Theme = "system" | "light" | "dark";

function readTheme(): Theme {
    try {
        const theme = localStorage.getItem(THEME_KEY);
        return theme === "light" || theme === "dark" ? theme : "system";
    } catch {
        return "system";
    }
}

function ThemePicker({ locale }: { locale: Locale }) {
    const [theme, setTheme] = useState(readTheme);
    const labels =
        locale === "zh"
            ? { appearance: "外观", system: "跟随系统", light: "浅色", dark: "深色" }
            : { appearance: "Appearance", system: "System", light: "Light", dark: "Dark" };

    useLayoutEffect(() => {
        if (theme === "system") delete document.documentElement.dataset.theme;
        else document.documentElement.dataset.theme = theme;
        try {
            localStorage.setItem(THEME_KEY, theme);
        } catch {
            /* The current theme still applies if storage is unavailable. */
        }
    }, [theme]);

    return (
        <div className="theme-picker">
            <Icon name={theme === "dark" ? "moon" : theme === "light" ? "sun" : "monitor"} />
            <select
                aria-label={labels.appearance}
                value={theme}
                onChange={({ target }) => {
                    const value = target.value;
                    if (value === "light" || value === "dark" || value === "system")
                        setTheme(value);
                }}
            >
                <option value="system">{labels.system}</option>
                <option value="light">{labels.light}</option>
                <option value="dark">{labels.dark}</option>
            </select>
        </div>
    );
}

function Illustration() {
    return (
        <svg className="hero-illustration" viewBox="0 0 510 240" fill="none" aria-hidden="true">
            <circle cx="261" cy="132" r="97" fill="var(--tint)" />
            <circle cx="261" cy="132" r="121" stroke="var(--line)" strokeDasharray="3 9" />
            <g transform="rotate(-10 185 122)">
                <rect
                    x="102"
                    y="50"
                    width="180"
                    height="132"
                    rx="19"
                    fill="var(--success-bg)"
                    stroke="var(--line-strong)"
                />
                <rect x="119" y="70" width="33" height="33" rx="10" fill="var(--surface-alt)" />
                <path
                    d="M128 87h15m-11-5-4 5 4 5m7-10 4 5-4 5"
                    stroke="var(--success)"
                    strokeWidth="2"
                    strokeLinecap="round"
                />
                <path
                    d="M121 127h80M121 141h120M121 155h49"
                    stroke="var(--line-strong)"
                    strokeWidth="6"
                    strokeLinecap="round"
                />
            </g>
            <g transform="rotate(8 315 146)">
                <rect
                    x="221"
                    y="84"
                    width="185"
                    height="131"
                    rx="19"
                    fill="var(--surface)"
                    stroke="var(--line)"
                />
                <rect x="240" y="103" width="33" height="33" rx="10" fill="var(--surface-alt)" />
                <path
                    d="M249 114h15v13h-15zM254 110h6v4"
                    stroke="var(--muted)"
                    strokeWidth="1.8"
                    strokeLinejoin="round"
                />
                <path
                    d="M241 158h76M241 173h115M241 188h46"
                    stroke="var(--line)"
                    strokeWidth="6"
                    strokeLinecap="round"
                />
            </g>
            <circle cx="373" cy="56" r="25" fill="var(--action)" />
            <path
                d="m362 56 7 7 14-15"
                stroke="var(--on-action)"
                strokeWidth="3"
                strokeLinecap="round"
                strokeLinejoin="round"
            />
            <path
                d="M83 107h12m-6-6v12M422 165h12m-6-6v12"
                stroke="var(--muted)"
                strokeWidth="2"
                strokeLinecap="round"
            />
            <circle cx="128" cy="207" r="4" fill="var(--warning)" />
        </svg>
    );
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

    return (
        <section className="login-card" aria-labelledby="login-title">
            <div className="card-emblem">
                <Icon name="lock" />
            </div>
            <h2 id="login-title">{t.welcome}</h2>
            <p className="muted login-intro">{t.loginIntro}</p>
            <form onSubmit={submit} aria-busy={pending}>
                <div className="field">
                    <label htmlFor="username">{t.username}</label>
                    <input
                        id="username"
                        name="username"
                        autoComplete="username"
                        autoCapitalize="none"
                        spellCheck={false}
                        required
                        maxLength={64}
                        placeholder={t.usernamePlaceholder}
                        value={username}
                        disabled={pending}
                        onChange={(event) => {
                            setUsername(event.target.value);
                            setError(null);
                        }}
                    />
                </div>
                <div className="field">
                    <label htmlFor="password">{t.password}</label>
                    <div className="password-input">
                        <input
                            id="password"
                            name="password"
                            type={visible ? "text" : "password"}
                            autoComplete="current-password"
                            required
                            maxLength={128}
                            placeholder={t.passwordPlaceholder}
                            value={password}
                            disabled={pending}
                            onChange={(event) => {
                                setPassword(event.target.value);
                                setError(null);
                            }}
                        />
                        <button
                            type="button"
                            className="password-toggle"
                            aria-label={visible ? t.hidePassword : t.showPassword}
                            aria-pressed={visible}
                            disabled={pending}
                            onClick={() => setVisible(!visible)}
                        >
                            <Icon name={visible ? "eyeOff" : "eye"} />
                        </button>
                    </div>
                </div>
                {error && (
                    <p className="form-error" role="alert">
                        {feedback(error, t)}
                    </p>
                )}
                <button className="primary-button" type="submit" disabled={pending}>
                    {pending ? (
                        <>
                            <span className="spinner" />
                            {t.signingIn}
                        </>
                    ) : (
                        <>
                            {t.signIn}
                            <Icon name="arrow" />
                        </>
                    )}
                </button>
            </form>
            <p className="login-hint">
                <Icon name="lock" />
                {t.adminHint}
            </p>
        </section>
    );
}

type AuthState =
    | { status: "checking" }
    | { status: "anonymous" }
    | { status: "unavailable"; reason: ApiErrorKind }
    | { status: "authenticated"; session: Session };
export function App() {
    const [commands] = useState(() => new PendingCommandController());
    const [uploads] = useState(() => new PendingUploadController());
    const [confirmations] = useState(() => new PendingConfirmationController());
    const [ocrJobs] = useState(() => new OcrJobIntents());
    const [copies] = useState(() => new OcrCopyController());
    const [periods] = useState(() => new PendingPeriodController());
    const [masters] = useState(() => new PendingMasterDataController());
    const [drafts] = useState(() => new PendingBusinessDraftController());
    const [recurring] = useState(() => new PendingRecurringController());
    const [businessConfirmations] = useState(() => new PendingBusinessConfirmationController());
    const [settlements] = useState(() => new PendingBusinessSettlementController());
    const [credits] = useState(() => new PendingBusinessCreditController());
    const [invoices] = useState(() => new PendingInvoiceController());
    const [invoiceProfiles] = useState(() => new PendingInvoiceProfiles());
    const [interledger] = useState(() => new PendingInterledger());
    const [prices] = useState(() => new PendingPrice());
    const [reminders] = useState(() => new PendingReminderController());
    const [reminderEmail] = useState(() => new PendingReminderEmail());
    const [locale, setLocale] = useState<Locale>(readLocale);
    const [auth, setAuth] = useState<AuthState>({ status: "checking" });
    const [retryKey, setRetryKey] = useState(0);
    const [loggingOut, setLoggingOut] = useState(false);
    const [logoutFailed, setLogoutFailed] = useState(false);
    const t = copy[locale];

    useEffect(() => {
        document.documentElement.lang = locale === "zh" ? "zh-CN" : "en";
        try {
            localStorage.setItem(LOCALE_KEY, locale);
        } catch {
            /* Language still works if storage is unavailable. */
        }
    }, [locale]);

    useEffect(() => {
        const controller = new AbortController();
        setAuth({ status: "checking" });
        void getSession(controller.signal)
            .then((session) => {
                if (!controller.signal.aborted) {
                    commands.setOwner(String(session.user.id));
                    uploads.setOwner(String(session.user.id));
                    ocrJobs.setOwner(String(session.user.id));
                    confirmations.setOwner(String(session.user.id));
                    copies.setOwner(String(session.user.id));
                    periods.setOwner(String(session.user.id));
                    masters.setOwner(String(session.user.id));
                    drafts.setOwner(String(session.user.id));
                    recurring.setOwner(String(session.user.id));
                    reminders.setOwner(String(session.user.id));
                    reminderEmail.setOwner(String(session.user.id));
                    businessConfirmations.setOwner(String(session.user.id));
                    settlements.setOwner(String(session.user.id));
                    credits.setOwner(String(session.user.id));
                    invoices.setOwner(String(session.user.id));
                    invoiceProfiles.setOwner(String(session.user.id));
                    interledger.setOwner(String(session.user.id));
                    prices.setOwner(String(session.user.id));
                    setAuth({ status: "authenticated", session });
                }
            })
            .catch((problem: unknown) => {
                if (controller.signal.aborted) return;
                if (problem instanceof ApiError && problem.kind === "unauthorized") {
                    commands.setOwner(null);
                    uploads.setOwner(null);
                    ocrJobs.setOwner(null);
                    confirmations.setOwner(null);
                    copies.setOwner(null);
                    periods.setOwner(null);
                    masters.setOwner(null);
                    drafts.setOwner(null);
                    recurring.setOwner(null);
                    reminders.setOwner(null);
                    reminderEmail.setOwner(null);
                    businessConfirmations.setOwner(null);
                    settlements.setOwner(null);
                    credits.setOwner(null);
                    invoices.setOwner(null);
                    invoiceProfiles.setOwner(null);
                    interledger.setOwner(null);
                    prices.setOwner(null);
                    setAuth({ status: "anonymous" });
                } else
                    setAuth({
                        status: "unavailable",
                        reason: problem instanceof ApiError ? problem.kind : "server",
                    });
            });
        return () => controller.abort();
    }, [
        retryKey,
        commands,
        uploads,
        ocrJobs,
        confirmations,
        copies,
        periods,
        masters,
        drafts,
        recurring,
        reminders,
        reminderEmail,
        businessConfirmations,
        settlements,
        credits,
        invoices,
        invoiceProfiles,
        interledger,
        prices,
    ]);

    useEffect(() => {
        if (auth.status === "checking" || auth.status === "unavailable") return;
        const path = auth.status === "authenticated" ? "/" : "/login";
        if (window.location.pathname !== path) window.history.replaceState(null, "", path);
        document.title =
            auth.status === "authenticated" ? `${t.overview} · Coinpup` : `${t.welcome} · Coinpup`;
    }, [auth.status, t.overview, t.welcome]);

    async function logout() {
        if (auth.status !== "authenticated" || loggingOut) return;
        setLoggingOut(true);
        setLogoutFailed(false);
        try {
            await signOut(auth.session.csrf_token);
            commands.setOwner(null, true);
            uploads.setOwner(null, true);
            ocrJobs.setOwner(null, true);
            confirmations.setOwner(null, true);
            copies.setOwner(null, true);
            periods.setOwner(null, true);
            masters.setOwner(null, true);
            drafts.setOwner(null, true);
            recurring.setOwner(null, true);
            reminders.setOwner(null, true);
            reminderEmail.setOwner(null, true);
            businessConfirmations.setOwner(null, true);
            settlements.setOwner(null, true);
            credits.setOwner(null, true);
            invoices.setOwner(null, true);
            invoiceProfiles.setOwner(null, true);
            interledger.setOwner(null, true);
            prices.setOwner(null, true);
            setAuth({ status: "anonymous" });
        } catch (problem) {
            if (problem instanceof ApiError && problem.kind === "unauthorized") {
                commands.setOwner(null, true);
                uploads.setOwner(null, true);
                ocrJobs.setOwner(null, true);
                confirmations.setOwner(null, true);
                copies.setOwner(null, true);
                periods.setOwner(null, true);
                masters.setOwner(null, true);
                drafts.setOwner(null, true);
                recurring.setOwner(null, true);
                reminders.setOwner(null, true);
                reminderEmail.setOwner(null, true);
                businessConfirmations.setOwner(null, true);
                settlements.setOwner(null, true);
                credits.setOwner(null, true);
                invoices.setOwner(null, true);
                invoiceProfiles.setOwner(null, true);
                interledger.setOwner(null, true);
                prices.setOwner(null, true);
                setAuth({ status: "anonymous" });
            } else setLogoutFailed(true);
        } finally {
            setLoggingOut(false);
        }
    }

    return (
        <div className={`app ${auth.status === "authenticated" ? "app-workspace" : "app-entry"}`}>
            <a className="skip-link" href="#main">
                {t.skip}
            </a>
            <header className="topbar">
                <Brand linked={auth.status !== "authenticated"} />
                <div className="topbar-actions">
                    <LanguageSwitch locale={locale} onChange={setLocale} t={t} />
                    <ThemePicker locale={locale} />
                    {auth.status === "authenticated" && (
                        <button
                            className="logout-button"
                            type="button"
                            aria-label={loggingOut ? t.signingOut : t.signOut}
                            onClick={() => void logout()}
                            disabled={loggingOut}
                        >
                            <Icon name="logout" />
                            <span>{loggingOut ? t.signingOut : t.signOut}</span>
                        </button>
                    )}
                </div>
            </header>
            {logoutFailed && (
                <div className="logout-error" role="alert">
                    {t.logoutError}
                </div>
            )}
            {auth.status === "authenticated" ? (
                <BusinessWorkspace
                    session={auth.session}
                    locale={locale}
                    commands={commands}
                    uploads={uploads}
                    ocrJobs={ocrJobs}
                    confirmations={confirmations}
                    copies={copies}
                    periods={periods}
                    masters={masters}
                    drafts={drafts}
                    recurring={recurring}
                    reminders={reminders}
                    reminderEmail={reminderEmail}
                    businessConfirmations={businessConfirmations}
                    settlements={settlements}
                    credits={credits}
                    invoices={invoices}
                    invoiceProfiles={invoiceProfiles}
                    interledger={interledger}
                    prices={prices}
                    onSessionChanged={(previous, session) => {
                        setAuth((current) =>
                            current.status === "authenticated" &&
                            current.session.csrf_token === previous.csrf_token &&
                            current.session.user.id === session.user.id
                                ? { status: "authenticated", session }
                                : current,
                        );
                    }}
                    onUnauthorized={() => {
                        commands.setOwner(null);
                        uploads.setOwner(null);
                        ocrJobs.setOwner(null);
                        confirmations.setOwner(null);
                        copies.setOwner(null);
                        periods.setOwner(null);
                        masters.setOwner(null);
                        drafts.setOwner(null);
                        recurring.setOwner(null);
                        reminders.setOwner(null);
                        reminderEmail.setOwner(null);
                        businessConfirmations.setOwner(null);
                        settlements.setOwner(null);
                        credits.setOwner(null);
                        invoices.setOwner(null);
                        invoiceProfiles.setOwner(null);
                        interledger.setOwner(null);
                        prices.setOwner(null);
                        setAuth({ status: "anonymous" });
                    }}
                />
            ) : (
                <>
                    <main id="main" className="entry-main">
                        <section className="entry-story">
                            <span className="preview-badge">
                                <span className="tiny-dot" />
                                {t.preview}
                            </span>
                            <p className="eyebrow">{t.privateSpace}</p>
                            <h1>
                                {t.heroTitle}
                                <br />
                                <span>{t.heroAccent}</span>
                            </h1>
                            <p className="hero-description">{t.heroDescription}</p>
                            <Illustration />
                            <div className="hero-features">
                                <div>
                                    <Icon name="wallet" />
                                    <div>
                                        <strong>{t.separateBooks}</strong>
                                        <p>{t.separateDescription}</p>
                                    </div>
                                </div>
                                <div>
                                    <Icon name="globe" />
                                    <div>
                                        <strong>{t.allTogether}</strong>
                                        <p>{t.allDescription}</p>
                                    </div>
                                </div>
                            </div>
                        </section>
                        <div className="entry-form-area">
                            {auth.status === "anonymous" ? (
                                <LoginForm
                                    t={t}
                                    onSuccess={(session) => {
                                        commands.setOwner(String(session.user.id));
                                        uploads.setOwner(String(session.user.id));
                                        ocrJobs.setOwner(String(session.user.id));
                                        confirmations.setOwner(String(session.user.id));
                                        copies.setOwner(String(session.user.id));
                                        periods.setOwner(String(session.user.id));
                                        masters.setOwner(String(session.user.id));
                                        drafts.setOwner(String(session.user.id));
                                        recurring.setOwner(String(session.user.id));
                                        reminders.setOwner(String(session.user.id));
                                        reminderEmail.setOwner(String(session.user.id));
                                        businessConfirmations.setOwner(String(session.user.id));
                                        settlements.setOwner(String(session.user.id));
                                        credits.setOwner(String(session.user.id));
                                        invoices.setOwner(String(session.user.id));
                                        invoiceProfiles.setOwner(String(session.user.id));
                                        interledger.setOwner(String(session.user.id));
                                        prices.setOwner(String(session.user.id));
                                        setLogoutFailed(false);
                                        setAuth({ status: "authenticated", session });
                                    }}
                                />
                            ) : (
                                <section className="login-card connection-card" aria-live="polite">
                                    <div className="card-emblem">
                                        {auth.status === "checking" ? (
                                            <span className="spinner" />
                                        ) : (
                                            <Icon name="server" />
                                        )}
                                    </div>
                                    <h2>
                                        {auth.status === "checking"
                                            ? t.checkingSession
                                            : t.connectionTitle}
                                    </h2>
                                    <p className="muted">
                                        {auth.status === "checking"
                                            ? t.checkingDescription
                                            : feedback(auth.reason, t)}
                                    </p>
                                    {auth.status === "unavailable" && (
                                        <button
                                            className="primary-button"
                                            type="button"
                                            onClick={() => setRetryKey((key) => key + 1)}
                                        >
                                            {t.retry}
                                            <Icon name="refresh" />
                                        </button>
                                    )}
                                </section>
                            )}
                            <p className="entry-scope">{t.currentScope}</p>
                        </div>
                    </main>
                    <footer className="entry-footer">
                        <span>© {new Date().getFullYear()} Coinpup</span>
                        <span>{t.footer}</span>
                    </footer>
                </>
            )}
        </div>
    );
}
