export type IconName =
    | "arrow"
    | "lock"
    | "eye"
    | "eyeOff"
    | "grid"
    | "wallet"
    | "receipt"
    | "chart"
    | "logout"
    | "refresh"
    | "check"
    | "server"
    | "database"
    | "globe"
    | "company"
    | "personal"
    | "ledger"
    | "transfer"
    | "scale"
    | "link"
    | "calendar"
    | "briefcase"
    | "draft"
    | "credits"
    | "bell"
    | "tag"
    | "sliders"
    | "folder"
    | "scan"
    | "coins"
    | "menu"
    | "chevronUp"
    | "sun"
    | "moon"
    | "monitor";
const paths: Record<IconName, string[]> = {
    transfer: ["M3 8h16m-4-4 4 4-4 4M21 16H5m4-4-4 4 4 4"],
    scale: ["M12 3v17m-4 1h8M3 7l18-2M6 7l-4 7h8L6 7Zm12-1-4 7h8l-4-7Z"],
    link: ["m10 14 4-4m-5 1-2 2a3.5 3.5 0 1 0 5 5l3-3m0-2 2-2a3.5 3.5 0 0 0-5-5l-3 3"],
    calendar: ["M4 5h16v15H4zM8 3v4m8-4v4M4 10h16m-12 5h3"],
    briefcase: ["M3 7h18v13H3zM8 7V4h8v3M3 12q9 5 18 0M10 13v3h4v-3"],
    draft: ["M4 20l5-1L20 8l-4-4L5 15l-1 5Zm9-13 4 4"],
    credits: ["M21 19v-4a5 5 0 0 0-5-5H4m6-6-6 6 6 6"],
    bell: ["M4 17l2-3v-4a6 6 0 0 1 12 0v4l2 3H4Zm5 3a3 3 0 0 0 6 0"],
    tag: ["M3 4h9l9 9-8 8L3 11V4Zm4 4h.01"],
    sliders: ["M3 7h10m4 0h4M3 17h4m4 0h10M13 4h4v6h-4zM7 14h4v6H7z"],
    folder: ["M3 7V5h7l2 3h9v12H3V7Z"],
    scan: ["M3 9V4h5m8 0h5v5m0 6v5h-5m-8 0H3v-5m4-6h10M7 14h7"],
    coins: [
        "M16 6c0 2-3 3-6 3S4 8 4 6s3-3 6-3 6 1 6 3ZM4 6v5c0 2 3 3 6 3m6-8v3m-4 4c0 2 2 3 5 3s5-1 5-3-2-3-5-3-5 1-5 3Zm0 0v5c0 2 2 3 5 3s5-1 5-3v-5",
    ],
    menu: ["M4 6h16M4 12h16M4 18h16"],
    chevronUp: ["m5 15 7-7 7 7"],
    sun: [
        "M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1.5 1.5m11 11L19 19M5 19l1.5-1.5m11-11L19 5",
        "M12 17a5 5 0 1 0 0-10 5 5 0 1 0 0 10Z",
    ],
    moon: ["M20 15.7a9 9 0 0 1-11.7-11.7A9 9 0 1 0 20 15.7Z"],
    monitor: ["M3 4h18v13H3zM12 17v4M8 21h8"],
    company: [
        "M3 21h18M5 21V4l11-2v19M16 9h4v12M8 7h1m3-1h1M8 11h1m3-1h1M8 15h1m3-1h1M10 21v-3h2v3",
    ],
    personal: ["M16 8a4 4 0 1 1-8 0 4 4 0 0 1 8 0ZM4 21v-2a7 7 0 0 1 7-7h2a7 7 0 0 1 7 7v2"],
    ledger: ["M5 4h14a1 1 0 0 1 1 1v16H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2ZM4 17h16M9 4v13M12 8h5"],
    arrow: ["M5 12h14m-5-5 5 5-5 5"],
    lock: ["M7 10V7a5 5 0 0 1 10 0v3", "M5 10h14v11H5z", "M12 14v3"],
    eye: ["M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12Z", "M15 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0Z"],
    eyeOff: [
        "m3 3 18 18",
        "M10 5.2A9 9 0 0 1 12 5c6 0 10 7 10 7a20 20 0 0 1-3 3.8",
        "M6.5 6.5C3.8 8.7 2 12 2 12s4 7 10 7a10 10 0 0 0 5.5-1.5",
        "M9.9 9.9a3 3 0 0 0 4.2 4.2",
    ],
    grid: ["M3 3h7v7H3zM14 3h7v7h-7zM3 14h7v7H3zM14 14h7v7h-7z"],
    wallet: ["M20 8V5H5a2 2 0 0 0 0 4h16v11H5a2 2 0 0 1-2-2V7", "M21 12h-5v4h5"],
    receipt: ["m5 3 3 2 4-2 4 2 3-2v18l-3-2-4 2-4-2-3 2V3Z", "M9 9h6M9 13h6"],
    chart: ["M4 4v16h16", "M8 15v-4M13 15V7M18 15v-6"],
    logout: ["M9 4H4v16h5", "M8 12h12m-4-4 4 4-4 4"],
    refresh: [
        "M20 7v5h-5",
        "M4 17v-5h5",
        "M5.1 8a8 8 0 0 1 13.3-3L20 7M4 17l1.6 2A8 8 0 0 0 19 16",
    ],
    check: ["m5 12 4 4L19 6"],
    server: ["M3 4h18v6H3zM3 14h18v6H3z", "M7 7h.01M7 17h.01M11 7h6M11 17h6"],
    database: [
        "M20 6c0 2-3.6 4-8 4s-8-2-8-4 3.6-4 8-4 8 2 8 4Z",
        "M4 6v12c0 2 3.6 4 8 4s8-2 8-4V6",
        "M4 12c0 2 3.6 4 8 4s8-2 8-4",
    ],
    globe: ["M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z", "M3 12h18M12 3c4 5 4 13 0 18-4-5-4-13 0-18Z"],
};

export function Icon({ name, className = "" }: { name: IconName; className?: string }) {
    return (
        <svg
            className={`icon ${className}`}
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.7"
            strokeLinecap="round"
            strokeLinejoin="round"
            aria-hidden="true"
        >
            {paths[name].map((path, index) => (
                <path d={path} key={index} />
            ))}
        </svg>
    );
}
