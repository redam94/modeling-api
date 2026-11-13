/**
 * The localStorage key of the recent items of the global search (`features/search/recents.ts`).
 * It is here, without imports, so sign-out (`features/auth/gate.tsx`) can remove the lists and
 * not load the search: the share link and the client portal must not hold `features/search`.
 */
export const RECENTS_STORAGE_KEY = "mapi.search.recent";
