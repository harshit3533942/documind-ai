/** @type {import('tailwindcss').Config} */
// Light UI: white / very light tint backgrounds, dark ink text, brand colour
// used only for accents (borders, icons, tinted panels). No dark theme.
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        ink: { DEFAULT: "#10213F", muted: "#44546F", soft: "#5B6B85" },
        brand: {
          50: "#EEF4FF", 100: "#DCE7FE", 200: "#BCD0FB", 300: "#93B2F6",
          500: "#3B6FE0", 600: "#2856C7", 700: "#1F44A0", 800: "#1B3A84",
        },
        page: "#F6F8FC",
      },
      fontFamily: {
        sans: ["Inter", "Segoe UI", "system-ui", "-apple-system", "sans-serif"],
      },
    },
  },
  plugins: [],
};
