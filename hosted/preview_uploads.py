"""Bounded upload overlap, used only when no local reel needs preview files."""
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait


class PreviewUploads:
    def __init__(self, upload, performance, capacity=4, on_wait=None, on_progress=None):
        self.upload = upload
        self.performance = performance
        self.capacity = capacity
        self.on_wait = on_wait
        self.on_progress = on_progress
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="preview-upload")
        self.pending = set()
        self.results = {}
        self.closed = False

    def _collect(self, block=False):
        finished = ({future for future in self.pending if future.done()} if not block else
                    wait(self.pending, timeout=1, return_when=FIRST_COMPLETED)[0])
        if block and not finished and self.on_wait:
            self.on_wait()
        for future in finished:
            self.pending.remove(future)
            value, size = future.result()
            self.results[value[0].event_id] = value, size
        if self.on_progress:
            self.on_progress(len(self.results))

    def submit(self, event, path):
        if self.closed:
            raise RuntimeError("Preview upload queue is closed")
        self._collect()
        while len(self.pending) >= self.capacity:
            self._collect(block=True)
        # Snapshot before returning; the render coordinator owns the Event.
        event = event.model_copy(deep=True)
        self.pending.add(self.executor.submit(self.upload, event, path, True))
        self.performance.observe("preview_upload_queue_depth", len(self.pending))

    def finish(self):
        if self.closed:
            return
        try:
            while self.pending:
                self._collect(block=True)
        finally:
            self.closed = True
            self.executor.shutdown(wait=True, cancel_futures=True)
            self.pending.clear()
