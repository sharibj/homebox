import { computed, onUnmounted, ref } from "vue";
import { ApiError, type createApi } from "./api";
import { createCapturePoller } from "./capture-poller";
import type { Capture, CaptureJob } from "./types";

export function useCaptureJobs(options: {
  api: ReturnType<typeof createApi>;
  context: () => unknown;
  online: () => boolean;
  receive: (capture: Capture) => void;
  boundaryError: (cause: unknown) => void;
}) {
  const job = ref<CaptureJob | null>(null);
  const jobs = ref<CaptureJob[]>([]);
  const submissionKey = ref("");
  const issue = ref("");
  const uploading = ref(false);
  const pending = computed(() => uploading.value || Boolean(submissionKey.value && !job.value)
    || job.value?.status === "queued" || job.value?.status === "running");
  const applied = new Set<string>();
  const consumed = new Set<string>();
  function setUrl() {
    const url = new URL(window.location.href);
    if (job.value) url.searchParams.set("captureJob", job.value.id);
    else url.searchParams.delete("captureJob");
    if (submissionKey.value && !job.value) url.searchParams.set("captureSubmission", submissionKey.value);
    else url.searchParams.delete("captureSubmission");
    window.history.replaceState(null, "", url);
  }
  function remember(value: CaptureJob) {
    jobs.value = [value, ...jobs.value.filter(previous => previous.id !== value.id)].slice(0, 20);
  }
  function receive(value: CaptureJob): boolean {
    if (value.captureId && consumed.has(value.captureId)) return false;
    job.value = value; submissionKey.value = value.submissionKey; issue.value = "";
    remember(value); setUrl();
    const intentKey = `${String(options.context())}:${value.id}`;
    if (["succeeded", "manual"].includes(value.status) && value.result && !applied.has(intentKey)) {
      applied.add(intentKey); options.receive(value.result);
    }
    if (value.status === "failed") issue.value = value.error?.message || "Photo analysis failed. Your entered details are unchanged. Nothing was added to inventory.";
    if (value.status === "expired") issue.value = "This photo has expired. Your entered details are unchanged. Choose a new photo or continue manually without it.";
    return value.status === "queued" || value.status === "running";
  }
  function isBoundary(cause: unknown) {
    return cause instanceof ApiError && (cause.status === 401 || cause.status === 403 || cause.code === "COLLECTION_CONTEXT_CHANGED");
  }
  const poller = createCapturePoller<CaptureJob | null>({
    context: options.context,
    async read(id, signal) {
      if (!options.online()) throw new ApiError(0, "offline", "You’re offline.");
      if (id.startsWith("submission:")) {
        const response = await options.api<{ jobs: CaptureJob[] }>(`/capture-jobs?submissionKey=${encodeURIComponent(id.slice(11))}`, { signal, timeoutMs: 15_000 });
        return response.jobs[0] || null;
      }
      return options.api<CaptureJob>(`/capture-jobs/${encodeURIComponent(id)}`, { signal, timeoutMs: 15_000 });
    },
    receive(value) {
      if (!value) {
        issue.value = "The upload response was not received and no matching photo job is visible yet. Checking again without resending the photo. No inventory item was created.";
        return true;
      }
      const hadJob = Boolean(job.value);
      const keepPolling = receive(value);
      if (!hadJob && keepPolling) { poller.start(value.id); return false; }
      return keepPolling;
    },
    failed(cause) {
      if (isBoundary(cause)) { options.boundaryError(cause); return false; }
      if (cause instanceof ApiError && cause.status === 404) {
        job.value = null; submissionKey.value = ""; setUrl();
        issue.value = "This photo job is unavailable or expired in this session or collection. Your entered details are unchanged. Nothing was retried.";
        return false;
      }
      if (cause instanceof ApiError && ["stale_response", "request_cancelled"].includes(cause.code)) return false;
      issue.value = "Could not check photo progress. Keeping this job and checking again when connected. Nothing was resent or added to inventory.";
      return true;
    },
  });
  function detach() {
    poller.stop(); job.value = null; submissionKey.value = ""; issue.value = ""; uploading.value = false;
    setUrl();
  }
  function reset() { detach(); jobs.value = []; applied.clear(); consumed.clear(); }
  function consume(captureId: string) {
    consumed.add(captureId);
    jobs.value = jobs.value.filter(value => value.captureId !== captureId);
    if (job.value?.captureId === captureId) detach();
  }
  function select(value: CaptureJob) {
    poller.stop();
    if (receive(value)) poller.start(value.id);
  }
  async function submit(form: FormData) {
    if (pending.value) return;
    poller.stop();
    const context = options.context();
    const key = crypto.randomUUID();
    submissionKey.value = key; job.value = null; uploading.value = true; issue.value = ""; setUrl();
    try {
      const value = await options.api<CaptureJob>("/capture-jobs", { method: "POST", body: form, idempotencyKey: key });
      if (context !== options.context()) return;
      select(value);
    } catch (cause) {
      if (context !== options.context()) return;
      if (isBoundary(cause)) throw cause;
      if (cause instanceof ApiError && ([400, 413, 415, 422].includes(cause.status) || cause.code === "idempotency_conflict")) {
        submissionKey.value = ""; setUrl(); throw cause;
      }
      issue.value = "The upload response was interrupted. Checking for the existing photo job without uploading again. No inventory item was created.";
      poller.start(`submission:${key}`);
    } finally {
      if (context === options.context()) uploading.value = false;
    }
  }
  async function recover() {
    poller.stop();
    const context = options.context();
    const url = new URL(window.location.href);
    const id = url.searchParams.get("captureJob");
    const key = url.searchParams.get("captureSubmission");
    const response = await options.api<{ jobs: CaptureJob[] }>("/capture-jobs", { timeoutMs: 15_000 });
    if (context !== options.context()) return;
    jobs.value = response.jobs.filter(value => !value.captureId || !consumed.has(value.captureId));
    if (id && /^[A-Za-z0-9_-]{1,160}$/.test(id)) {
      try {
        const value = await options.api<CaptureJob>(`/capture-jobs/${encodeURIComponent(id)}`, { timeoutMs: 15_000 });
        if (context === options.context()) select(value);
      } catch (cause) {
        if (!(cause instanceof ApiError && cause.status === 404)) throw cause;
        detach();
        issue.value = "That previous photo job is unavailable or expired in this session or collection. Your entered details are unchanged. Nothing was retried.";
      }
    } else if (key && /^[A-Za-z0-9_-]{1,160}$/.test(key)) {
      submissionKey.value = key;
      const found = await options.api<{ jobs: CaptureJob[] }>(`/capture-jobs?submissionKey=${encodeURIComponent(key)}`, { timeoutMs: 15_000 });
      if (context !== options.context()) return;
      if (found.jobs[0]) select(found.jobs[0]);
      else {
        issue.value = "No matching photo job is visible yet. Checking the submitted upload without resending it. You can dismiss photo progress to use manual entry.";
        poller.start(`submission:${key}`);
      }
    } else {
      const active = response.jobs.find(value => value.status === "queued" || value.status === "running");
      if (active) select(active);
    }
  }
  onUnmounted(() => poller.stop());
  return { job, jobs, issue, uploading, pending, submit, recover, select, detach, reset, consume };
}
