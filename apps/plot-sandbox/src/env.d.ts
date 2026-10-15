/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Comma-separated origins allowed to embed the sandbox (the web app). */
  readonly VITE_APP_ORIGINS?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
