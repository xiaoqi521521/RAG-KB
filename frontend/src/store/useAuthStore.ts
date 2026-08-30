import { create } from 'zustand';

export const TOKEN_STORAGE_KEY = 'ragkb.token';
export const USER_STORAGE_KEY = 'ragkb.user';

export interface AuthUser {
  username: string;
}

interface AuthState {
  user: AuthUser | null;
  token: string | null;
  setAuth: (user: AuthUser, token: string) => void;
  logout: () => void;
  isAuthenticated: () => boolean;
}

function readUser(): AuthUser | null {
  try {
    return JSON.parse(localStorage.getItem(USER_STORAGE_KEY) || 'null') as AuthUser | null;
  } catch {
    return null;
  }
}

export const useAuthStore = create<AuthState>((set, get) => ({
  user: readUser(),
  token: localStorage.getItem(TOKEN_STORAGE_KEY),

  setAuth: (user, token) => {
    localStorage.setItem(USER_STORAGE_KEY, JSON.stringify(user));
    localStorage.setItem(TOKEN_STORAGE_KEY, token);
    set({ user, token });
  },

  logout: () => {
    localStorage.removeItem(USER_STORAGE_KEY);
    localStorage.removeItem(TOKEN_STORAGE_KEY);
    set({ user: null, token: null });
  },

  isAuthenticated: () => !!get().token,
}));
