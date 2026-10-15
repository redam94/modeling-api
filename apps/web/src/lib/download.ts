/** Save a validated image data URL (from the plot sandbox) as a file. */
export function saveDataUrl(dataUrl: string, filename: string): void {
  const m = /^data:(image\/png|image\/svg\+xml);base64,([A-Za-z0-9+/]*={0,2})$/.exec(dataUrl);
  if (!m) throw new Error("refusing to save an unexpected data URL");
  const bin = atob(m[2] ?? "");
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  const url = URL.createObjectURL(new Blob([bytes], { type: m[1] }));
  const a = document.createElement("a");
  a.href = url;
  a.download = filename.replace(/[^A-Za-z0-9_.-]+/g, "_");
  a.rel = "noopener";
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
}
