/**
 * The deterministic STORE zip of collection parts, against a vector built field by field from
 * the zip specification (APPNOTE 4.3.7 local header, 4.3.12 central directory header, 4.3.16
 * end of central directory record) and the bytes that Python's `zipfile` writes for the same
 * two entries (`ZipInfo(name, (1980, 1, 1, 0, 0, 0))`, `create_system = 0`, `writestr`).
 */
import { describe, expect, it } from "vitest";

import { blobBytes, buildStoreZip, crc32, crc32Update, DOS_DATE_1980, storeZipSize, ZipError } from "./zip-store";

const enc = new TextEncoder();

/** Little-endian bytes of `value` in `n` bytes. */
function le(value: number, n: 2 | 4): number[] {
  return Array.from({ length: n }, (_, i) => Math.floor(value / 256 ** i) % 256);
}

function ascii(s: string): number[] {
  return [...enc.encode(s)];
}

function local(name: string, data: string, crc: number): number[] {
  return [
    ...le(0x04034b50, 4), // local file header signature
    ...le(20, 2), // version needed to extract (2.0)
    ...le(0, 2), // general purpose bit flag
    ...le(0, 2), // compression method: stored
    ...le(0, 2), // last mod file time 00:00:00
    ...le(0x0021, 2), // last mod file date 1980-01-01
    ...le(crc, 4),
    ...le(data.length, 4), // compressed size
    ...le(data.length, 4), // uncompressed size
    ...le(name.length, 2),
    ...le(0, 2), // extra field length
    ...ascii(name),
    ...ascii(data),
  ];
}

function central(name: string, data: string, crc: number, offset: number): number[] {
  return [
    ...le(0x02014b50, 4), // central file header signature
    ...le(20, 2), // version made by (MS-DOS, 2.0)
    ...le(20, 2), // version needed to extract
    ...le(0, 2), // flags
    ...le(0, 2), // method
    ...le(0, 2), // time
    ...le(0x0021, 2), // date
    ...le(crc, 4),
    ...le(data.length, 4),
    ...le(data.length, 4),
    ...le(name.length, 2),
    ...le(0, 2), // extra field length
    ...le(0, 2), // file comment length
    ...le(0, 2), // disk number start
    ...le(0, 2), // internal file attributes
    ...le(0o600 * 65536, 4), // external file attributes (mode 0600 << 16)
    ...le(offset, 4), // relative offset of the local header
    ...ascii(name),
  ];
}

function end(count: number, size: number, offset: number): number[] {
  return [...le(0x06054b50, 4), ...le(0, 2), ...le(0, 2), ...le(count, 2), ...le(count, 2), ...le(size, 4), ...le(offset, 4), ...le(0, 2)];
}

const PYTHON_HEX =
  "504b0304140000000000000021002639f4cb090000000900000005000000612e706e67313233343536373839" +
  "504b03041400000000000000210086a61036050000000500000005000000622e706e6768656c6c6f" +
  "504b01021400140000000000000021002639f4cb0900000009000000050000000000000000000000800100000000612e706e67" +
  "504b010214001400000000000000210086a61036050000000500000005000000000000000000000080012c000000622e706e67" +
  "504b0506000000000200020066000000540000000000";

const bytesOf = blobBytes;

function hex(bytes: Uint8Array | number[]): string {
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

describe("crc32", () => {
  it("gives the known check values", () => {
    expect(crc32(enc.encode("123456789"))).toBe(0xcbf43926);
    expect(crc32(enc.encode("hello"))).toBe(0x3610a686);
    expect(crc32(new Uint8Array(0))).toBe(0);
    expect(crc32(enc.encode("The quick brown fox jumps over the lazy dog"))).toBe(0x414fa339);
  });

  it("continues over chunks", () => {
    const all = enc.encode("123456789");
    expect(crc32Update(crc32(all.subarray(0, 4)), all.subarray(4))).toBe(0xcbf43926);
  });
});

describe("buildStoreZip", () => {
  const a = { name: "a.png", data: "123456789", crc: 0xcbf43926 };
  const b = { name: "b.png", data: "hello", crc: 0x3610a686 };

  it("writes the byte-exact layout of the specification, entries sorted by name", async () => {
    // given in the wrong order: the zip sorts them
    const blob = buildStoreZip([
      { name: b.name, blob: new Blob([b.data]), crc32: b.crc },
      { name: a.name, blob: new Blob([a.data]), crc32: a.crc },
    ]);
    const la = local(a.name, a.data, a.crc);
    const lb = local(b.name, b.data, b.crc);
    const ca = central(a.name, a.data, a.crc, 0);
    const cb = central(b.name, b.data, b.crc, la.length);
    const expected = [...la, ...lb, ...ca, ...cb, ...end(2, ca.length + cb.length, la.length + lb.length)];
    const got = await bytesOf(blob);
    expect(hex(got)).toBe(hex(expected));
    expect(got.length).toBe(208);
    expect(hex(got)).toBe(PYTHON_HEX);
    expect(blob.type).toBe("application/zip");
    expect(storeZipSize([{ nameLength: 5, size: 9 }, { nameLength: 5, size: 5 }])).toBe(208);
    expect(DOS_DATE_1980).toBe(33);
  });

  it("is deterministic and makes an empty zip of no entries", async () => {
    const entries = [{ name: "x.jpg", blob: new Blob([new Uint8Array([1, 2, 3])]), crc32: crc32(new Uint8Array([1, 2, 3])) }];
    expect(hex(await bytesOf(buildStoreZip(entries)))).toBe(hex(await bytesOf(buildStoreZip(entries))));
    expect(hex(await bytesOf(buildStoreZip([])))).toBe(hex(end(0, 0, 0)));
  });

  it("refuses duplicate and non-ASCII names", () => {
    const e = { name: "a.png", blob: new Blob(["x"]), crc32: 0 };
    expect(() => buildStoreZip([e, e])).toThrow(ZipError);
    expect(() => buildStoreZip([{ ...e, name: "é.png" }])).toThrow(/not printable ASCII/);
  });
});
