/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_API_BASE?: string;
  readonly VITE_PLOTS_ORIGIN?: string;
  readonly VITE_AUTH_BASE?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
