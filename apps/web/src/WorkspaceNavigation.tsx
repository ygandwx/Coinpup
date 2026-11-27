import { useEffect, useRef, useState } from "react";
import { Icon } from "./Icon";
import type { IconName } from "./Icon";
import type { Locale } from "./i18n";

const groups = [
    {
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
    zh: string;
    en: string;
    items: readonly (readonly [string, IconName])[];
}[];

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
                {groups.map((group) => (
                    <div
                        className="nav-group"
                        key={group.en}
                        role="group"
                        aria-label={group[locale]}
                    >
                        <span className="nav-group-name" aria-hidden="true">
                            {group[locale]}
                        </span>
                        {group.items.map(([item, icon]) => (
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
                                            document.getElementById("business-title")?.focus(),
                                        );
                                    }
                                }}
                            >
                                <Icon name={icon} />
                                {labels[item]}
                            </button>
                        ))}
                    </div>
                ))}
            </div>
        </nav>
    );
}
