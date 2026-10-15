/**
 * Upload a file straight to the signed PUT URL of an upload session. The API never sees the
 * bytes; the URL points into the quarantine bucket (GCS shim locally).
 */
import type { UploadSession } from "./types";

export interface UploadProgress {
  loaded: number;
  total: number;
}

export class UploadError extends Error {
  override name = "UploadError";
  constructor(
    message: string,
    readonly status: number | null,
  ) {
    super(message);
  }
}

/** Headers the signed URL was minted with; they must be sent verbatim. */
export function uploadHeaders(session: Pick<UploadSession, "upload">): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [k, v] of Object.entries(session.upload.headers ?? {})) {
    // Browsers refuse to set forbidden headers (content-length, host, ...); GCS computes them.
    if (/^(content-length|host|connection)$/i.test(k)) continue;
    out[k] = v;
  }
  return out;
}

export function putToSignedUrl(
  session: Pick<UploadSession, "upload" | "size_bytes">,
  file: Blob,
  onProgress?: (p: UploadProgress) => void,
  signal?: AbortSignal,
): Promise<void> {
  const { upload } = session;
  if (upload.method !== "PUT") return Promise.reject(new UploadError("unsupported upload method", null));
  if (upload.max_bytes != null && file.size > upload.max_bytes) {
    return Promise.reject(new UploadError(`file is larger than the ${upload.max_bytes} byte limit`, null));
  }
  if (new Date(upload.expires_at).getTime() <= Date.now()) {
    return Promise.reject(new UploadError("the upload URL has expired; start a new upload", null));
  }
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", upload.url);
    xhr.withCredentials = false;
    for (const [k, v] of Object.entries(uploadHeaders(session))) xhr.setRequestHeader(k, v);
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) onProgress?.({ loaded: e.loaded, total: e.total });
    };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) resolve();
      else reject(new UploadError(`upload failed with HTTP ${xhr.status}`, xhr.status));
    };
    xhr.onerror = () => reject(new UploadError("the storage endpoint could not be reached", null));
    xhr.onabort = () => reject(new UploadError("upload cancelled", null));
    signal?.addEventListener("abort", () => xhr.abort(), { once: true });
    xhr.send(file);
  });
}
