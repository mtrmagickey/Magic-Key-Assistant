/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ["./templates/**/*.html"],
  theme: {
    extend: {
      fontFamily: { sans: ['Inter', 'system-ui', 'sans-serif'] },
      colors: {
        midnight: '#1a1714',
        forest: '#2B2622',
        teal:  { DEFAULT: '#EFAAC4', light: '#F4C3D6', dark: '#C8829E' },
        cyan:  { DEFAULT: '#7EA3CC', light: '#9EBCDA', dark: '#5E85B0' },
        gold:  { DEFAULT: '#83781B', light: '#A89C3A' },
        coral: '#E13943',
      }
    }
  },
  plugins: [],
}
