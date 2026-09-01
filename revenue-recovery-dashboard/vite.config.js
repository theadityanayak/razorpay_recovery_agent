import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// strictPort matters here. main.py's CORS allow_origins is an exact-match list
// containing only http://localhost:5173 and http://127.0.0.1:5173. If Vite
// silently fell back to 5174 because 5173 was taken, every request would fail
// CORS and the dashboard would look "broken" for a reason with no obvious
// symptom. Failing to start is the better error.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true,
    host: "localhost",
  },
  preview: {
    port: 5173,
    strictPort: true,
  },
});
