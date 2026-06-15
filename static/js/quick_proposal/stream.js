/**
 * SSE client for quick proposal runs.
 * Returns the EventSource so callers can close it early if needed.
 *
 * The stream stays open across all pipeline phases — do NOT close on
 * phase_complete. It closes naturally when the server sends the sentinel
 * (connection drops), or on a terminal error.
 */
export function startStream(runId, handlers = {}) {
    const {
        onPageReady,
        onPhaseStart,
        onPhaseComplete,
        onIndexLoaded,
        onPageClassified,
        onExtractionMessage,
        onIndexUpdate,
        onRegionPreview,
        onError,
        onDone,
    } = handlers;

    const es = new EventSource(`/api/quick_proposal/stream/${runId}`);

    es.addEventListener('page_ready', e => {
        onPageReady?.(JSON.parse(e.data));
    });

    es.addEventListener('phase_start', e => {
        onPhaseStart?.(JSON.parse(e.data));
    });

    es.addEventListener('phase_complete', e => {
        // Do NOT close here — more phases follow.
        onPhaseComplete?.(JSON.parse(e.data));
    });

    es.addEventListener('index_loaded', e => {
        onIndexLoaded?.(JSON.parse(e.data));
    });

    es.addEventListener('page_classified', e => {
        onPageClassified?.(JSON.parse(e.data));
    });

    es.addEventListener('extraction_message', e => {
        onExtractionMessage?.(JSON.parse(e.data));
    });

    es.addEventListener('index_update', e => {
        onIndexUpdate?.(JSON.parse(e.data));
    });

    es.addEventListener('region_preview', e => {
        onRegionPreview?.(JSON.parse(e.data));
    });

    let hadError = false;

    es.addEventListener('error', e => {
        // Named SSE event "error" emitted by the server pipeline.
        if (e.data) {
            hadError = true;
            onError?.(JSON.parse(e.data));
        }
        es.close();
    });

    // Connection-level close: server finished (sentinel) or network drop.
    es.onerror = () => {
        if (es.readyState === EventSource.CLOSED) {
            if (!hadError) onDone?.();
            return;
        }
        onError?.({ message: 'Stream connection lost', phase: null });
        es.close();
    };

    return es;
}