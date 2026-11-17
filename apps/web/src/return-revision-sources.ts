import { ApiError } from "./api";
import { getAllocationReturnCapacity } from "./allocation-return-api";
import type { AllocationReturnDetail, AllocationReturnCapacity } from "./allocation-return-api";
import { listBusinessAllocations } from "./business-settlement-api";
import type { ReturnCurrent } from "./allocation-return-revision-api";
import { date } from "./settlement-revision-facts";
import { uuid } from "./refund-intent";

const invalid = () => new ApiError("server", 200, "invalid_response");
const pageSize = 25;
export async function loadReturnSourcePage(
    original: AllocationReturnDetail,
    current: ReturnCurrent,
    offset: number,
    signal: AbortSignal,
    io = { list: listBusinessAllocations, capacity: getAllocationReturnCapacity },
) {
    const { ledger_id: ledger, payment_id: payment, payment_asset_id: asset } = current;
    const rows = await io.list(ledger, payment, offset, signal);
    if (
        !Array.isArray(rows) ||
        rows.length > pageSize ||
        rows.some(
            (r) =>
                !uuid(r.id) ||
                r.ledger_id !== ledger ||
                r.payment_id !== payment ||
                r.asset_id !== asset,
        ) ||
        new Set(rows.map((r) => r.id)).size !== rows.length
    )
        throw invalid();
    const ids = new Set(rows.map((r) => r.id));
    if (offset === 0) {
        // Load retained original sources immediately, even if their allocations are on older pages.
        const retained = new Set(current.members.map((m) => m.source_allocation_line_id));
        for (const line of original.lines)
            if (retained.has(line.source_allocation_line_id)) ids.add(line.source_allocation_id);
    }
    const ordered = [...ids],
        capacities: AllocationReturnCapacity[] = [];
    for (let i = 0; i < ordered.length; i += 4) {
        signal.throwIfAborted();
        const batch = await Promise.all(
            ordered.slice(i, i + 4).map(async (id) => {
                if (!uuid(id)) throw invalid();
                const c = await io.capacity(ledger, id, signal);
                if (
                    c.id !== id ||
                    c.ledger_id !== ledger ||
                    c.payment_id !== payment ||
                    c.asset_id !== asset ||
                    !date(c.allocation_date) ||
                    !Array.isArray(c.lines) ||
                    !c.lines.length ||
                    c.lines.length > 200 ||
                    c.lines.some(
                        (line) =>
                            !uuid(line.source_allocation_line_id) ||
                            !uuid(line.source_operation_id) ||
                            !uuid(line.document_id) ||
                            !uuid(line.document_line_id) ||
                            typeof line.document_asset_id !== "string" ||
                            typeof line.description !== "string",
                    ) ||
                    new Set(c.lines.map((line) => line.source_allocation_line_id)).size !==
                        c.lines.length
                )
                    throw invalid();
                return c;
            }),
        );
        capacities.push(...batch);
    }
    return { capacities, more: rows.length === pageSize && offset < 100000 };
}
