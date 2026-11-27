import { useEffect, useRef, useState } from "react";
import { Icon } from "./Icon";
import type { IconName } from "./Icon";
import type { Locale } from "./i18n";

const groups = [
    {
        id: "daybook",
        zh: "日常记账",
        en: "Daybook",
        items: [
            ["overview", "grid"],
            ["accounts", "wallet"],
            ["transactions", "transfer"],
            ["files", "folder"],
            ["ocr", "scan"],
        ],
    },
    {
        id: "business",
        zh: "经营业务",
        en: "Business",
        items: [
            ["masters", "briefcase"],
            ["drafts", "draft"],
            ["business-confirmations", "check"],
            ["invoices", "receipt"],
            ["business-settlements", "transfer"],
            ["business-credits", "credits"],
            ["controls", "scale"],
            ["interledger", "link"],
        ],
    },
    {
        id: "reporting",
        zh: "报表与计划",
        en: "Reporting & planning",
        items: [
            ["valuation", "chart"],
            ["periods", "calendar"],
            ["recurring", "refresh"],
            ["reminders", "bell"],
        ],
    },
    {
        id: "administration",
        zh: "账本管理",
        en: "Administration",
        items: [
            ["categories", "tag"],
            ["details", "ledger"],
            ["assets", "coins"],
            ["settings", "sliders"],
        ],
    },
] as const satisfies readonly {
    id: string;
    zh: string;
    en: string;
    items: readonly (readonly [string, IconName])[];
}[];

const NAV_KEY = "coinpup.nav-collapsed";
type GroupId = (typeof groups)[number]["id"];

function readCollapsedGroups(): GroupId[] {
    try {
        const stored = (localStorage.getItem(NAV_KEY) ?? "").split(",");
        return groups.filter(({ id }) => stored.includes(id)).map(({ id }) => id);
    } catch {
        return [];
    }
}

export type WorkspaceView = (typeof groups)[number]["items"][number][0];

export function isWorkspaceView(candidate: string | null): candidate is WorkspaceView {
    return groups.some((group) => group.items.some(([view]) => view === candidate));
}

export function WorkspaceNavigation({
    locale,
    labels,
    view,
    disabled,
    onSelect,
}: {
    locale: Locale;
    labels: Record<WorkspaceView, string>;
    view: WorkspaceView;
    disabled: boolean;
    onSelect: (view: WorkspaceView) => void;
}) {
    const [expanded, setExpanded] = useState(false);
    const [collapsed, setCollapsed] = useState(readCollapsedGroups);
    useEffect(() => {
        try {
            localStorage.setItem(NAV_KEY, collapsed.join(","));
        } catch {
            /* The navigation still works if storage is unavailable. */
        }
    }, [collapsed]);
    const list = useRef<HTMLDivElement>(null);

    useEffect(() => {
        const container = list.current;
        const current = container?.querySelector<HTMLElement>('[aria-current="page"]');
        if (expanded || !container || !current) return;
        const keepCurrentVisible = () => {
            if (container.scrollWidth <= container.clientWidth) return;
            const bounds = container.getBoundingClientRect();
            const button = current.getBoundingClientRect();
            if (button.left < bounds.left) container.scrollLeft += button.left - bounds.left;
            else if (button.right > bounds.right)
                container.scrollLeft += button.right - bounds.right;
        };
        keepCurrentVisible();
        window.addEventListener("resize", keepCurrentVisible);
        return () => window.removeEventListener("resize", keepCurrentVisible);
    }, [expanded, view, locale]);

    return (
        <nav
            className="business-navigation"
            aria-label={locale === "zh" ? "工作区导航" : "Workspace navigation"}
            data-expanded={expanded}
        >
            <button
                className="nav-toggle"
                type="button"
                aria-controls="workspace-views"
                aria-expanded={expanded}
                aria-label={
                    expanded
                        ? locale === "zh"
                            ? "收起栏目"
                            : "Collapse sections"
                        : locale === "zh"
                          ? "展开全部栏目"
                          : "Show all sections"
                }
                onClick={() => setExpanded(!expanded)}
            >
                <Icon name={expanded ? "chevronUp" : "menu"} />
            </button>
            <div className="nav-groups" ref={list} id="workspace-views">
                {groups.map(({ id, items, ...group }) => {
                    const isCollapsed = collapsed.includes(id);
                    const active = items.some(([item]) => item === view);
                    const pagesId = `workspace-views-${id}`;
                    return (
                        <div className="nav-group" key={id} role="group" aria-label={group[locale]}>
                            <button
                                className="nav-group-toggle"
                                type="button"
                                aria-expanded={!isCollapsed}
                                aria-controls={pagesId}
                                data-current={active && isCollapsed ? "true" : undefined}
                                onClick={() =>
                                    setCollapsed((current) =>
                                        current.includes(id)
                                            ? current.filter((value) => value !== id)
                                            : [...current, id],
                                    )
                                }
                            >
                                {group[locale]}
                                <Icon name="chevronUp" />
                            </button>
                            <div
                                className="nav-group-pages"
                                id={pagesId}
                                data-collapsed={isCollapsed}
                            >
                                {items.map(([item, icon]) => (
                                    <button
                                        key={item}
                                        type="button"
                                        disabled={disabled}
                                        aria-current={view === item ? "page" : undefined}
                                        onClick={() => {
                                            onSelect(item);
                                            setExpanded(false);
                                            if (expanded) {
                                                requestAnimationFrame(() =>
                                                    document
                                                        .getElementById("business-title")
                                                        ?.focus(),
                                                );
                                            }
                                        }}
                                    >
                                        <Icon name={icon} />
                                        {labels[item]}
                                    </button>
                                ))}
                            </div>
                        </div>
                    );
                })}
            </div>
        </nav>
    );
}
