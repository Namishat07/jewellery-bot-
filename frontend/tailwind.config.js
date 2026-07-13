/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,jsx}'],
  theme: {
    extend: {
      colors: {
        ink: '#17140F',
        surface: '#211C15',
        surfaceRaised: '#2A241A',
        brass: '#C9A659',
        brassBright: '#E0C280',
        garnet: '#7A2E2E',
        ivory: '#F2ECDF',
        muted: '#A79C88',
        hairline: '#3A3226',
      },
      fontFamily: {
        display: ['Fraunces', 'serif'],
        sans: ['Manrope', 'sans-serif'],
        mono: ['"Space Mono"', 'monospace'],
      },
      keyframes: {
        facetGlow: {
          '0%, 100%': { transform: 'rotate(0deg)' },
          '50%': { transform: 'rotate(180deg)' },
        },
        fadeUp: {
          '0%': { opacity: 0, transform: 'translateY(8px)' },
          '100%': { opacity: 1, transform: 'translateY(0)' },
        },
      },
      animation: {
        facetGlow: 'facetGlow 14s linear infinite',
        fadeUp: 'fadeUp 0.4s ease-out',
      },
    },
  },
  plugins: [],
}
