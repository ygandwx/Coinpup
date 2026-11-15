/** Match Python str.strip without changing other submitted text. */
export function stripCommandText(value: string): string {
    const blank = (char: string) =>
        /^\p{White_Space}$/u.test(char) || (char >= "\u001c" && char <= "\u001f");
    let start = 0,
        end = value.length;
    while (start < end && blank(value[start])) start += 1;
    while (end > start && blank(value[end - 1])) end -= 1;
    return value.slice(start, end);
}

/** Python and PostgreSQL count Unicode code points; each uses at most two JS code units. */
export function validBusinessReason(value: unknown): boolean {
    return (
        typeof value === "string" &&
        value.length <= 4000 &&
        [...value].length <= 2000 &&
        !value.includes("\0") &&
        stripCommandText(value) !== ""
    );
}
