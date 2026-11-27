export function LoadingSkeleton({ label }: { label: string }) {
    return (
        <div className="dashboard-skeleton" role="status" aria-label={label}>
            <span aria-hidden="true" />
            <span aria-hidden="true" />
            <span aria-hidden="true" />
        </div>
    );
}
