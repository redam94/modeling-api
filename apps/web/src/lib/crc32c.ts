/**
 * CRC32C (Castagnoli), as computed by GCS and the shim, and its base64 big-endian form
 * (`crc32c` on ObjectMeta / ArtifactLink). Used to detect corrupted artifact downloads.
 */

const TABLE = (() => {
  const t = new Uint32Array(256);
  for (let i = 0; i < 256; i++) {
    let c = i;
    for (let k = 0; k < 8; k++) c = c & 1 ? (c >>> 1) ^ 0x82f63b78 : c >>> 1;
    t[i] = c >>> 0;
  }
  return t;
})();

export function crc32c(data: Uint8Array, seed = 0): number {
  let crc = ~seed >>> 0;
  for (let i = 0; i < data.length; i++) {
    crc = (TABLE[(crc ^ (data[i] ?? 0)) & 0xff] ?? 0) ^ (crc >>> 8);
  }
  return ~crc >>> 0;
}

export function crc32cBase64(data: Uint8Array): string {
  const v = crc32c(data);
  const bytes = [(v >>> 24) & 0xff, (v >>> 16) & 0xff, (v >>> 8) & 0xff, v & 0xff];
  return btoa(String.fromCharCode(...bytes));
}
