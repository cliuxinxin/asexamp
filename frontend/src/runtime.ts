/// <reference types="vite/client" />
export const cloudRuntime = import.meta.env?.VITE_RUNTIME === 'cloud';
