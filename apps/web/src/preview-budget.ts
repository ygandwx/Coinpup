/** Canvas dimensions only; amounts never enter this calculation. */
export function previewSize(width: number, height: number) {
    if (![width, height].every((n) => Number.isFinite(n) && n > 0 && n <= 100000))
        throw new Error("preview_dimensions");
    const scale = Math.min(1600 / width, 2400 / height, Math.sqrt(4_000_000 / (width * height)));
    return {
        width: Math.max(1, Math.floor(width * scale)),
        height: Math.max(1, Math.floor(height * scale)),
        scale,
    };
}
