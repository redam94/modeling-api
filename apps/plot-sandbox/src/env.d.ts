/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Comma-separated origins allowed to embed the sandbox (the web app). */
  readonly VITE_APP_ORIGINS?: string;
  /** Origin of the API the embed page reads `/v1/embeds/<token>` from (build time). */
  readonly VITE_API_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
