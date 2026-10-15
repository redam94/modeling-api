/**
 * Download artifacts through their short-lived signed URLs and verify integrity against the
 * CRC32C the API reported (corruption detection; the authoritative check is the server's
 * signed-manifest verification before it mints the link).
 */
import type { ArtifactLink } from "@/api/types";

import { crc32cBase64 } from "./crc32c";

export const MAX_ARTIFACT_BYTES = 64 * 1024 * 1024;
export const MAX_JSON_BYTES = 16 * 1024 * 1024;

export class ArtifactError extends Error {
  override name = "ArtifactError";
}

export async function fetchArtifactBytes(
  link: Pick<ArtifactLink, "url" | "size" | "crc32c" | "name">,
  opts: { maxBytes?: number; signal?: AbortSignal; fetchImpl?: typeof fetch } = {},
): Promise<Uint8Array> {
  const max = opts.maxBytes ?? MAX_ARTIFACT_BYTES;
  if (link.size > max) throw new ArtifactError(`${link.name} is too large to open here (${link.size} bytes)`);
  const res = await (opts.fetchImpl ?? fetch)(link.url, {
    credentials: "omit",
    referrerPolicy: "no-referrer",
    ...(opts.signal ? { signal: opts.signal } : {}),
  });
  if (!res.ok) throw new ArtifactError(`download of ${link.name} failed with HTTP ${res.status}`);
  const bytes = new Uint8Array(await res.arrayBuffer());
  if (bytes.length > max) throw new ArtifactError(`${link.name} exceeded the size limit`);
  if (bytes.length !== link.size) {
    throw new ArtifactError(`${link.name}: expected ${link.size} bytes, received ${bytes.length}`);
  }
  const crc = crc32cBase64(bytes);
  if (crc !== link.crc32c) throw new ArtifactError(`${link.name}: checksum mismatch (corrupted download)`);
  return bytes;
}

export async function fetchArtifactJson(
  link: Pick<ArtifactLink, "url" | "size" | "crc32c" | "name">,
  opts: { signal?: AbortSignal; fetchImpl?: typeof fetch } = {},
): Promise<unknown> {
  const bytes = await fetchArtifactBytes(link, { ...opts, maxBytes: MAX_JSON_BYTES });
  try {
    return JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes)) as unknown;
  } catch {
    throw new ArtifactError(`${link.name} is not valid UTF-8 JSON`);
  }
}
