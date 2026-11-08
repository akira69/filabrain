import { readFile, writeFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'

const { build } = await import(process.env.ESBUILD_MODULE || 'esbuild')
const read = file => readFile(new URL(file, import.meta.url), 'utf8')
const result = await build({
  entryPoints: [fileURLToPath(new URL('./main.ts', import.meta.url))],
  bundle: true,
  format: 'iife',
  minify: true,
  write: false,
  target: ['es2022'],
})
const css = await Promise.all(['page', 'chat', 'settings', 'batches'].map(name => read(name + '.css')))
let html = (await read('page.html')).replace('<!-- styles -->', () => css.join('\n'))
for (const section of ['chat', 'settings', 'batches']) {
  const fragment = await read(section + '.html')
  html = html.replace(`<!-- ${section} -->`, () => fragment)
}
const mark = (await read('../filabrain/icon.svg')).replace('<svg ', '<svg class="filabrain-mark" aria-hidden="true" ')
html = html.replaceAll('<!-- mark -->', () => mark)
html = html.replace('<!-- script -->', () => result.outputFiles[0].text.replace(/<\/script/gi, '<\\/script'))
await writeFile(new URL('../filabrain/page.html', import.meta.url), html)
