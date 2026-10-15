import { create } from "zustand";

export type ToastTone = "info" | "success" | "error";

export interface Toast {
  id: number;
  title: string;
  description?: string;
  tone: ToastTone;
}

interface ToastState {
  toasts: Toast[];
  push: (t: Omit<Toast, "id">) => void;
  dismiss: (id: number) => void;
}

let next = 1;

export const useToasts = create<ToastState>()((set, get) => ({
  toasts: [],
  push: (t) => {
    const id = next++;
    set({ toasts: [...get().toasts.slice(-4), { ...t, id }] });
    setTimeout(() => get().dismiss(id), t.tone === "error" ? 9000 : 4500);
  },
  dismiss: (id) => set({ toasts: get().toasts.filter((t) => t.id !== id) }),
}));

export const toast = {
  info: (title: string, description?: string) =>
    useToasts.getState().push({ title, tone: "info", ...(description ? { description } : {}) }),
  success: (title: string, description?: string) =>
    useToasts.getState().push({ title, tone: "success", ...(description ? { description } : {}) }),
  error: (title: string, description?: string) =>
    useToasts.getState().push({ title, tone: "error", ...(description ? { description } : {}) }),
};
