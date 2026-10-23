/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_API_BASE?: string;
  readonly VITE_PLOTS_ORIGIN?: string;
  readonly VITE_AUTH_BASE?: string;
  readonly VITE_TERMS_URL?: string;
  readonly VITE_ACCEPTABLE_USE_URL?: string;
  /** The documentation site the chat panel's "Learn more" links go to (v0.20). */
  readonly VITE_DOCS_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
