import { defineConfig } from 'vite'
import { svelte } from '@sveltejs/vite-plugin-svelte'
import tailwindcss from '@tailwindcss/vite'

// base: './' keeps asset URLs relative so the build works on a GitHub Pages
// project site (e.g. user.github.io/gaffer/) without knowing the repo name.
export default defineConfig({
  base: './',
  plugins: [svelte(), tailwindcss()],
})
