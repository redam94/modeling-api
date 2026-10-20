/**
 * v0.15: a deterministic zip of stored (not compressed) entries, the format of the image parts
 * of a collection upload. The bytes depend only on the entries: names sorted, every timestamp
 * DOS 1980-01-01 00:00, no extra fields, no comments, no data descriptors, version 2.0, made by
 * MS-DOS, external attributes `0o600 << 16`. Python's `zipfile.writestr` with
 * `ZipInfo(name, (1980, 1, 1, 0, 0, 0))` and `create_system = 0` writes the same bytes, so a
 * part that the web app builds again after a reload is byte-identical (the upload resumes at
 * part granularity).
 *
 * The entry bytes are never copied: the zip is a `Blob` of small header arrays and the files.
 * Parts stay below 2 GiB, so ZIP64 records are never necessary (the server refuses them).
 */

/** The DOS date of 1980-01-01: ((1980 - 1980) << 9) | (1 << 5) | 1. */
export const DOS_DATE_1980 = 0x0021;
/** The DOS time 00:00:00. */
export const DOS_TIME_0 = 0;
/** "Version needed to extract" and "version made by" (2.0, MS-DOS). */
export const ZIP_VERSION = 20;

const LOCAL_SIG = 0x04034b50;
const CENTRAL_SIG = 0x02014b50;
const END_SIG = 0x06054b50;
const LOCAL_HEADER = 30;
const CENTRAL_HEADER = 46;
const END_RECORD = 22;
const MAX_U16 = 0xffff;
const MAX_U32 = 0xffffffff;
/** Unix mode 0600 in the high half, as Python's `writestr` sets it. */
export const EXTERNAL_ATTRIBUTES = 0o600 << 16;

export interface ZipEntry {
  /** The entry name (ASCII; for parts `<sha256>.<ext>`). */
  name: string;
  blob: Blob;
  /** CRC-32 (IEEE) of the entry bytes, as an unsigned number. */
  crc32: number;
}

export class ZipError extends Error {
  override name = "ZipError";
}

let table: Uint32Array | null = null;

function crcTable(): Uint32Array {
  if (table) return table;
  table = new Uint32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    table[n] = c >>> 0;
  }
  return table;
}

/** Continue a CRC-32 over more bytes (`crc` is the value of the bytes before, 0 at the start). */
export function crc32Update(crc: number, bytes: Uint8Array): number {
  const t = crcTable();
  let c = (crc ^ MAX_U32) >>> 0;
  for (let i = 0; i < bytes.length; i++) c = t[(c ^ bytes[i]!) & 0xff]! ^ (c >>> 8);
  return (c ^ MAX_U32) >>> 0;
}

/** CRC-32 (IEEE 802.3, the zip checksum) of `bytes`, as an unsigned 32-bit number. */
export function crc32(bytes: Uint8Array): number {
  return crc32Update(0, bytes);
}

function asciiBytes(name: string): Uint8Array {
  const out = new Uint8Array(name.length);
  for (let i = 0; i < name.length; i++) {
    const c = name.charCodeAt(i);
    if (c < 0x20 || c > 0x7e) throw new ZipError(`the entry name ${JSON.stringify(name)} is not printable ASCII`);
    out[i] = c;
  }
  return out;
}

function localHeader(name: Uint8Array, crc: number, size: number): Uint8Array<ArrayBuffer> {
  const buf = new Uint8Array(LOCAL_HEADER + name.length);
  const v = new DataView(buf.buffer);
  v.setUint32(0, LOCAL_SIG, true);
  v.setUint16(4, ZIP_VERSION, true); // version needed to extract
  v.setUint16(6, 0, true); // flags
  v.setUint16(8, 0, true); // method: stored
  v.setUint16(10, DOS_TIME_0, true);
  v.setUint16(12, DOS_DATE_1980, true);
  v.setUint32(14, crc >>> 0, true);
  v.setUint32(18, size, true); // compressed size
  v.setUint32(22, size, true); // uncompressed size
  v.setUint16(26, name.length, true);
  v.setUint16(28, 0, true); // extra field length
  buf.set(name, LOCAL_HEADER);
  return buf;
}

function centralHeader(name: Uint8Array, crc: number, size: number, offset: number): Uint8Array<ArrayBuffer> {
  const buf = new Uint8Array(CENTRAL_HEADER + name.length);
  const v = new DataView(buf.buffer);
  v.setUint32(0, CENTRAL_SIG, true);
  v.setUint16(4, ZIP_VERSION, true); // version made by (MS-DOS, 2.0)
  v.setUint16(6, ZIP_VERSION, true); // version needed to extract
  v.setUint16(8, 0, true); // flags
  v.setUint16(10, 0, true); // method: stored
  v.setUint16(12, DOS_TIME_0, true);
  v.setUint16(14, DOS_DATE_1980, true);
  v.setUint32(16, crc >>> 0, true);
  v.setUint32(20, size, true);
  v.setUint32(24, size, true);
  v.setUint16(28, name.length, true);
  v.setUint16(30, 0, true); // extra field length
  v.setUint16(32, 0, true); // comment length
  v.setUint16(34, 0, true); // disk number start
  v.setUint16(36, 0, true); // internal attributes
  v.setUint32(38, EXTERNAL_ATTRIBUTES, true);
  v.setUint32(42, offset, true); // offset of the local header
  buf.set(name, CENTRAL_HEADER);
  return buf;
}

function endRecord(count: number, cdSize: number, cdOffset: number): Uint8Array<ArrayBuffer> {
  const buf = new Uint8Array(END_RECORD);
  const v = new DataView(buf.buffer);
  v.setUint32(0, END_SIG, true);
  v.setUint16(4, 0, true); // this disk
  v.setUint16(6, 0, true); // disk of the central directory
  v.setUint16(8, count, true);
  v.setUint16(10, count, true);
  v.setUint32(12, cdSize, true);
  v.setUint32(16, cdOffset, true);
  v.setUint16(20, 0, true); // comment length
  return buf;
}

/** The bytes of a blob (`Blob.arrayBuffer`, or a `FileReader` where a blob has no such method). */
export function blobBytes(blob: Blob): Promise<Uint8Array> {
  if (typeof blob.arrayBuffer === "function") return blob.arrayBuffer().then((b) => new Uint8Array(b));
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(new Uint8Array(reader.result as ArrayBuffer));
    reader.onerror = () => reject(reader.error ?? new Error("the file could not be read"));
    reader.readAsArrayBuffer(blob);
  });
}

/** The size in bytes of the zip that {@link buildStoreZip} makes of entries with these name lengths and sizes. */
export function storeZipSize(entries: readonly { nameLength: number; size: number }[]): number {
  let total = END_RECORD;
  for (const e of entries) total += LOCAL_HEADER + CENTRAL_HEADER + 2 * e.nameLength + e.size;
  return total;
}

/**
 * A zip of stored entries, sorted by name (a `Blob`; the entry blobs are referenced, not
 * copied). Refuses duplicate or non-ASCII names and anything that would need ZIP64.
 */
export function buildStoreZip(entries: readonly ZipEntry[]): Blob {
  const sorted = [...entries].sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0));
  if (sorted.length > MAX_U16 - 1) throw new ZipError(`a zip part holds at most ${MAX_U16 - 1} entries`);
  const parts: BlobPart[] = [];
  const central: Uint8Array<ArrayBuffer>[] = [];
  let offset = 0;
  let previous: string | null = null;
  for (const e of sorted) {
    if (e.name === previous) throw new ZipError(`the entry ${JSON.stringify(e.name)} is in the zip two times`);
    previous = e.name;
    const name = asciiBytes(e.name);
    const size = e.blob.size;
    if (size > MAX_U32 || offset > MAX_U32) throw new ZipError("the zip part is too large (it would need ZIP64)");
    parts.push(localHeader(name, e.crc32, size), e.blob);
    central.push(centralHeader(name, e.crc32, size, offset));
    offset += LOCAL_HEADER + name.length + size;
  }
  const cdSize = central.reduce((n, c) => n + c.length, 0);
  if (offset + cdSize > MAX_U32) throw new ZipError("the zip part is too large (it would need ZIP64)");
  parts.push(...central, endRecord(sorted.length, cdSize, offset));
  return new Blob(parts, { type: "application/zip" });
}
