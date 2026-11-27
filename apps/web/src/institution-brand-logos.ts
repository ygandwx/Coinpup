// Vite emits the checked-in marks as same-origin static assets.
const logos = import.meta.glob<string>("./brand-logos/**/*.{svg,png}", {
    eager: true,
    import: "default",
    query: "?url&no-inline",
});

export function institutionLogoUrl(path?: string): string | undefined {
    return path ? logos[`./brand-logos/${path}`] : undefined;
}
