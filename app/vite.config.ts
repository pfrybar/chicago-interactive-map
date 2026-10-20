import { defineConfig } from 'vite'

export default defineConfig({
  // public/builds is a symlink to ../data/builds, where the pipeline writes tiles.
  server: {
    port: 5173,
    allowedHosts: ['web'], // the eval container reaches the dev server by its compose name
    fs: { allow: ['..'] }, // the app imports ../eval/viewpoints.json
  },
  build: { target: 'es2022' },
})
