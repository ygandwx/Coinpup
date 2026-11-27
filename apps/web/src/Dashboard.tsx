import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";

import { ApiError } from "./api";
import { businessError } from "./business-errors";
import { EntityMark } from "./EntityMark";
import { Icon } from "./Icon";
import type { IconName } from "./Icon";
import type { Locale } from "./i18n";
import { listControlBalances } from "./ledger-api";
import type { ControlBalance, Entity } from "./ledger-api";
import { formatAssetAmount } from "./money";
import { LoadingSkeleton } from "./LoadingSkeleton";
import { listUpcomingReminders } from "./reminder-api";
import { readMvpReport } from "./valuation-api";
import "./dashboard.css";

type Translate = (zh: string, en: string) => string;
type Scope = { ledger: string; today: string };
type Result<T> = { data: T } | { error: unknown };
const payable = ["payable.supplier", "intercompany.payable"] as const;
const receivable = ["receivable.customer", "intercompany.receivable"] as const;
type DashboardLink = "accounts" | "details" | "valuation" | "controls" | "reminders";

const report = ({ ledger, today }: Scope, signal: AbortSignal) =>
    readMvpReport(
        {
            ledger_ids: [ledger],
            view: "operating",
            from_date: today.slice(0, 8) + "01",
            to_date: today,
            limit: 1,
        },
        signal,
    );

const balances = ({ ledger }: Scope, signal: AbortSignal) =>
    readMvpReport({ ledger_ids: [ledger], view: "balances", limit: 1 }, signal);

const due = async ({ ledger }: Scope, signal: AbortSignal) => {
    const readGroup = async (system_key: ControlBalance["system_key"]) => {
        const limit = 40;
        const rows: ControlBalance[] = [];
        for (let offset = 0; offset <= 100000; offset += limit) {
            const page = await listControlBalances(ledger, { system_key, limit, offset }, signal);
            rows.push(...page.filter((row) => !/^-?0(?:\.0+)?$/.test(row.amount)));
            if (rows.length >= 3 || page.length < limit) return rows.slice(0, 3);
        }
        throw new ApiError("server");
    };
    const readGroups = async (groups: readonly ControlBalance["system_key"][]) =>
        (await Promise.all(groups.map(readGroup))).flat();
    const [incoming, outgoing] = await Promise.all([readGroups(receivable), readGroups(payable)]);
    return { incoming, outgoing };
};

const reminders = ({ ledger, today }: Scope, signal: AbortSignal) =>
    listUpcomingReminders(ledger, today, signal);

function useDashboardRead<T>(
    read: (scope: Scope, signal: AbortSignal) => Promise<T>,
    ledger: string,
    today: string,
    refresh: number,
    onUnauthorized: () => void,
) {
    const [snapshot, setSnapshot] = useState<{
        ledger: string;
        today: string;
        refresh: number;
        result: Result<T>;
    }>();
    const unauthorized = useRef(onUnauthorized);
    useEffect(() => {
        unauthorized.current = onUnauthorized;
    }, [onUnauthorized]);
    useEffect(() => {
        const request = new AbortController();
        const save = (result: Result<T>) => {
            if (!request.signal.aborted) setSnapshot({ ledger, today, refresh, result });
        };
        void read({ ledger, today }, request.signal).then(
            (data) => save({ data }),
            (error: unknown) => {
                if (request.signal.aborted) return;
                if (error instanceof ApiError && error.status === 401) unauthorized.current();
                else save({ error });
            },
        );
        return () => request.abort();
    }, [ledger, today, refresh, read]);
    if (snapshot?.ledger === ledger && snapshot.today === today && snapshot.refresh === refresh)
        return snapshot.result;
    return undefined;
}

function Tile<T>({
    title,
    icon,
    state,
    locale,
    action,
    empty,
    children,
}: {
    title: string;
    icon: IconName;
    state: Result<T> | undefined;
    locale: Locale;
    action: ReactNode;
    empty: string;
    children: (data: T) => ReactNode;
}) {
    const t: Translate = (zh, en) => (locale === "zh" ? zh : en);
    const pending = state === undefined;
    return (
        <section className="dashboard-tile" aria-busy={pending}>
            <h2>
                <Icon name={icon} />
                {title}
            </h2>
            {pending ? (
                <LoadingSkeleton label={t("加载中", "Loading")} />
            ) : "error" in state ? (
                <p className="inline-error" role="alert">
                    {businessError(state.error, locale)}
                </p>
            ) : (
                (children(state.data) ?? <p className="dashboard-empty">{empty}</p>)
            )}
            {action}
        </section>
    );
}

function Quantity({
    amount,
    asset,
    locale,
    expense = false,
}: {
    amount: string;
    asset: string;
    locale: Locale;
    expense?: boolean;
}) {
    return (
        <span
            className="ui-amount dashboard-quantity"
            data-negative={expense || amount.startsWith("-")}
            aria-label={`${asset} ${amount}`}
            tabIndex={0}
        >
            {formatAssetAmount(amount, asset, locale)}
        </span>
    );
}

function DueList({
    rows,
    heading,
    outgoing,
    locale,
}: {
    rows: ControlBalance[];
    heading: string;
    outgoing?: boolean;
    locale: Locale;
}) {
    if (!rows.length) return null;
    return (
        <section>
            <h3>{heading}</h3>
            <ul className="dashboard-rows">
                {rows.map((row) => (
                    <li key={JSON.stringify(row)}>
                        <span>{row.asset_id}</span>
                        <Quantity
                            asset={row.asset_id}
                            locale={locale}
                            expense={outgoing}
                            amount={
                                outgoing
                                    ? row.amount.startsWith("-")
                                        ? row.amount.slice(1)
                                        : "-" + row.amount
                                    : row.amount
                            }
                        />
                    </li>
                ))}
            </ul>
        </section>
    );
}

export function Dashboard({
    entity,
    locale,
    refresh,
    disableNavigation,
    onNavigate,
    onUnauthorized,
}: {
    entity: Entity;
    locale: Locale;
    refresh: number;
    disableNavigation: boolean;
    onNavigate: (view: DashboardLink) => void;
    onUnauthorized: () => void;
}) {
    const t: Translate = (zh, en) => (locale === "zh" ? zh : en);
    const today = new Date().toISOString().slice(0, 10);
    const originals = useDashboardRead(balances, entity.ledger.id, today, refresh, onUnauthorized);
    const month = useDashboardRead(report, entity.ledger.id, today, refresh, onUnauthorized);
    const control = useDashboardRead(due, entity.ledger.id, today, refresh, onUnauthorized);
    const upcoming = useDashboardRead(reminders, entity.ledger.id, today, refresh, onUnauthorized);

    const link = (view: DashboardLink, label: string) => (
        <button
            type="button"
            className="dashboard-link secondary-button"
            disabled={disableNavigation}
            onClick={() => onNavigate(view)}
        >
            {label}
            <Icon name="arrow" />
        </button>
    );
    return (
        <div className="dashboard">
            <section className="dashboard-identity">
                <EntityMark entity={entity} />
                <div>
                    <h2>{entity.name}</h2>
                    <p>
                        {entity.kind === "company"
                            ? t("公司账本", "Company ledger")
                            : t("个人账本", "Personal ledger")}
                        {" · "}
                        {entity.ledger.base_asset_id}
                    </p>
                </div>
                {link("details", t("查看账本资料", "View ledger details"))}
            </section>
            <Tile
                title={t("原币余额", "Original balances")}
                icon="wallet"
                state={originals}
                locale={locale}
                action={link("accounts", t("查看账户", "View accounts"))}
                empty={t(
                    "添加账户并登记期初后，即可在这里查看原币余额。",
                    "Add an account and record an opening balance to see your original balances.",
                )}
            >
                {(data) =>
                    data.originals.length ? (
                        <>
                            <p className="dashboard-period">
                                {t(
                                    "含归档账户和停用资产的历史余额。",
                                    "Includes historical balances for archived accounts and disabled assets.",
                                )}
                            </p>
                            <ul className="dashboard-rows">
                                {data.originals.map(({ asset_id, amount }) => (
                                    <li key={asset_id}>
                                        <span>{asset_id}</span>
                                        <Quantity
                                            asset={asset_id}
                                            amount={amount}
                                            locale={locale}
                                        />
                                    </li>
                                ))}
                            </ul>
                        </>
                    ) : null
                }
            </Tile>
            <Tile
                title={t("本月经营收支", "Operating income and expenses this month")}
                icon="chart"
                state={month}
                locale={locale}
                action={link("valuation", t("查看报表", "View reports"))}
                empty={t(
                    "本月暂无经营收支。",
                    "There is no operating income or expense this month.",
                )}
            >
                {(data) =>
                    data.originals.length ? (
                        <>
                            <p className="dashboard-period">{today.slice(0, 7)} · UTC</p>
                            <ul className="dashboard-rows">
                                {data.originals.map(({ asset_id, inflow, outflow }) => (
                                    <li className="dashboard-flow" key={asset_id}>
                                        <strong>{asset_id}</strong>
                                        {inflow !== null && (
                                            <span>
                                                {t("收入", "Income")}{" "}
                                                <Quantity
                                                    asset={asset_id}
                                                    amount={inflow}
                                                    locale={locale}
                                                />
                                            </span>
                                        )}
                                        {outflow !== null && (
                                            <span>
                                                {t("支出", "Expense")}{" "}
                                                <Quantity
                                                    asset={asset_id}
                                                    amount={outflow}
                                                    locale={locale}
                                                    expense
                                                />
                                            </span>
                                        )}
                                    </li>
                                ))}
                            </ul>
                        </>
                    ) : null
                }
            </Tile>
            <Tile
                title={t("待收待付", "Receivables and payables")}
                icon="wallet"
                state={control}
                locale={locale}
                action={link("controls", t("查看全部往来", "View all control balances"))}
                empty={t(
                    "这个账本暂无应收或应付款项。",
                    "This ledger has no receivables or payables.",
                )}
            >
                {({ incoming, outgoing }) =>
                    incoming.length || outgoing.length ? (
                        <div className="dashboard-due">
                            <p className="dashboard-period">
                                {t(
                                    "每类最多显示三条未结余额。",
                                    "Shows up to three open balances per type.",
                                )}
                            </p>
                            <DueList
                                rows={incoming}
                                heading={t("待收", "Receivable")}
                                locale={locale}
                            />
                            <DueList
                                rows={outgoing}
                                heading={t("待付", "Payable")}
                                locale={locale}
                                outgoing
                            />
                        </div>
                    ) : null
                }
            </Tile>
            <Tile
                title={t("近期提醒", "Upcoming reminders")}
                icon="calendar"
                state={upcoming}
                locale={locale}
                action={link("reminders", t("管理提醒", "Manage reminders"))}
                empty={t(
                    "暂无即将到期的提醒。可在提醒中设置税务、年报或证件日期。",
                    "No upcoming reminders. Add due dates for tax, annual reports or documents.",
                )}
            >
                {(data) =>
                    data.length ? (
                        <ul className="dashboard-rows">
                            {data.map(({ id, title, effective_date }) => (
                                <li key={id}>
                                    <span>{title}</span>
                                    <time dateTime={effective_date ?? undefined}>
                                        {effective_date}
                                    </time>
                                </li>
                            ))}
                        </ul>
                    ) : null
                }
            </Tile>
        </div>
    );
}
