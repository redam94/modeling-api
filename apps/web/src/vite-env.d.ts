/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_API_BASE?: string;
  readonly VITE_PLOTS_ORIGIN?: string;
  readonly VITE_AUTH_BASE?: string;
  readonly VITE_TERMS_URL?: string;
  readonly VITE_ACCEPTABLE_USE_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
