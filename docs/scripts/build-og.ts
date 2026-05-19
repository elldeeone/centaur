#!/usr/bin/env bun
/*
 * build-og.ts
 *
 * Generates per-page Open Graph PNG cards at build time and writes them
 * into docs/public/og/. Ported from tempoxyz/mpp's runtime `/api/og`
 * handler — runs as a prebuild step instead of a serverless function so
 * the cards ship as static assets and serve identically in localhost,
 * staging, and production deployments.
 *
 * Inputs:
 *   - docs/scripts/og-template.svg   — body template with the named
 *       anchors <text id="CATEGORY">, <text id="Route title">, and
 *       <text id="Description …"> that we substitute per route.
 *   - docs/public/brand/og-image.svg — landing card (no substitution;
 *       rasterized once and used for `/` plus `_default.png`).
 *   - docs/pages/<path>.mdx          — frontmatter `title` + `description`.
 *   - docs/sidebar.ts                — derives CATEGORY from the page link.
 *
 * Output:
 *   - docs/public/og/<slug>.png      — 1200x657 raster per route.
 *   - docs/public/og/_default.png    — fallback used by Vocs when a
 *       route has no explicit `ogImageUrl` entry. Cloned from the
 *       homepage card so unknown routes still get on-brand artwork.
 *
 * Fonts: pulled from Google Fonts at build time and cached under
 * docs/scripts/.font-cache/ — no vendored binaries, no system font
 * lookup. Resolves URLs dynamically from the css2 endpoint because
 * gstatic versions the .ttf filenames per release.
 */

import { mkdirSync, readFileSync, readdirSync, statSync, writeFileSync, existsSync } from 'node:fs'
import { dirname, join, relative } from 'node:path'
import { fileURLToPath } from 'node:url'

import matter from 'gray-matter'
import { Resvg, initWasm } from '@resvg/resvg-wasm'

import { sidebar } from '../sidebar'

const __dirname = dirname(fileURLToPath(import.meta.url))
const DOCS_ROOT = join(__dirname, '..')
const PAGES_DIR = join(DOCS_ROOT, 'pages')
const OUT_DIR = join(DOCS_ROOT, 'public', 'og')
const TEMPLATE_SVG = readFileSync(join(__dirname, 'og-template.svg'), 'utf-8')
const HOME_SVG = readFileSync(join(DOCS_ROOT, 'public', 'brand', 'og-image.svg'), 'utf-8')

const FONT_CACHE_DIR = join(__dirname, '.font-cache')
mkdirSync(FONT_CACHE_DIR, { recursive: true })
mkdirSync(OUT_DIR, { recursive: true })

type SidebarItem = { text: string; link?: string; items?: SidebarItem[] }

// Recursively flatten sidebar into category + subcategory lookups keyed by
// the page link. A leaf at the top level of a group inherits the group's
// text as its category and has no subcategory.
function buildSidebarLookup() {
  const category: Record<string, string> = {}
  const subcategory: Record<string, string> = {}

  function walk(group: SidebarItem, parents: string[]) {
    if (!group.items) {
      if (group.link) {
        const [cat, sub] = parents
        if (cat) category[group.link] = cat
        if (sub) subcategory[group.link] = sub
      }
      return
    }
    const nextParents = [...parents, group.text]
    for (const item of group.items) walk(item, nextParents)
  }

  for (const group of sidebar as SidebarItem[]) walk(group, [])
  return { category, subcategory }
}

const { category: CATEGORY_MAP, subcategory: SUBCATEGORY_MAP } = buildSidebarLookup()

function getCategoryForPath(p: string): string | null {
  if (CATEGORY_MAP[p]) return CATEGORY_MAP[p]
  for (const [k, v] of Object.entries(CATEGORY_MAP)) {
    if (p.startsWith(`${k}/`)) return v
  }
  return null
}

function getSubcategoryForPath(p: string): string | null {
  if (SUBCATEGORY_MAP[p]) return SUBCATEGORY_MAP[p]
  for (const [k, v] of Object.entries(SUBCATEGORY_MAP)) {
    if (p.startsWith(`${k}/`)) return v
  }
  return null
}

/*
 * Google Fonts loader.
 *
 * Bodoni Moda — title type. High-contrast serif with a strong masthead
 *   feel that reads well at the 99px heading size.
 * Source Serif 4 — description / preview text. Adobe's editorial serif,
 *   reliably legible at 41px and pairs cleanly with Bodoni's curves.
 * Geist Mono — eyebrow ("CATEGORY"). Monospace eyebrow keeps the
 *   visual hierarchy clear without leaning on a proprietary font.
 *
 * We fetch the CSS first, then pull the ttf URL it advertises — gstatic
 * versions the path per release so a hardcoded URL stops resolving.
 */
const GOOGLE_FONT_FAMILIES = [
  { name: 'Bodoni Moda', cssParam: 'Bodoni+Moda:wght@400;500' },
  { name: 'Source Serif 4', cssParam: 'Source+Serif+4:wght@400;500' },
  { name: 'Geist Mono', cssParam: 'Geist+Mono:wght@400;500' },
]

async function loadGoogleFont(name: string, cssParam: string): Promise<Buffer> {
  const cached = join(FONT_CACHE_DIR, `${name.replace(/\s+/g, '-')}.ttf`)
  if (existsSync(cached)) return Buffer.from(readFileSync(cached))
  const cssUrl = `https://fonts.googleapis.com/css2?family=${cssParam}&display=swap`
  console.log(`Fetching font: ${name}`)
  // Google Fonts returns different ttf vs woff2 payloads based on the
  // User-Agent. Use a vanilla Mozilla UA so we get the ttf list (which
  // is what resvg-wasm consumes); woff2 would require fontverter again.
  const cssResponse = await fetch(cssUrl, { headers: { 'User-Agent': 'Mozilla/5.0' } })
  if (!cssResponse.ok) throw new Error(`Font CSS fetch failed for ${name}: ${cssResponse.status}`)
  const css = await cssResponse.text()
  const match = css.match(/url\((https:\/\/fonts\.gstatic\.com[^)]+\.ttf)\)/)
  if (!match) throw new Error(`No ttf URL found in Google Fonts CSS for ${name}`)
  const ttfResponse = await fetch(match[1])
  if (!ttfResponse.ok) throw new Error(`Font ttf fetch failed for ${name}: ${ttfResponse.status}`)
  const buffer = Buffer.from(await ttfResponse.arrayBuffer())
  writeFileSync(cached, buffer)
  return buffer
}

async function loadAllFonts(): Promise<Buffer[]> {
  const fonts: Buffer[] = []
  for (const { name, cssParam } of GOOGLE_FONT_FAMILIES) {
    fonts.push(await loadGoogleFont(name, cssParam))
  }
  return fonts
}

function esc(s: string): string {
  return s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
}

// Split a title into a balanced 1, 2, or 3 line wrap. Avoids leaving one
// word stranded on the second line by measuring against a target width
// based on average character width at the title's font size.
function balanceLines(text: string, fontSize: number, maxWidth = 1050): string[] {
  const words = text.split(' ')
  if (words.length <= 1) return [text]
  const avgCharWidth = fontSize * 0.58
  const charsPerLine = Math.floor(maxWidth / avgCharWidth)
  if (text.length <= charsPerLine) return [text]
  const needsThreeLines = text.length > charsPerLine * 2
  if (needsThreeLines && words.length >= 3) {
    const target = text.length / 3
    let bestI = 0,
      bestJ = 1,
      bestScore = Number.POSITIVE_INFINITY
    for (let i = 0; i < words.length - 2; i++) {
      const line1 = words.slice(0, i + 1).join(' ')
      for (let j = i + 1; j < words.length - 1; j++) {
        const line2 = words.slice(i + 1, j + 1).join(' ')
        const line3 = words.slice(j + 1).join(' ')
        const score =
          Math.abs(line1.length - target) +
          Math.abs(line2.length - target) +
          Math.abs(line3.length - target)
        if (score < bestScore) {
          bestScore = score
          bestI = i
          bestJ = j
        }
      }
    }
    return [
      words.slice(0, bestI + 1).join(' '),
      words.slice(bestI + 1, bestJ + 1).join(' '),
      words.slice(bestJ + 1).join(' '),
    ]
  }
  let bestSplit = 0,
    bestDiff = Number.POSITIVE_INFINITY
  for (let i = 0; i < words.length - 1; i++) {
    const left = words.slice(0, i + 1).join(' ')
    const right = words.slice(i + 1).join(' ')
    const diff = Math.abs(left.length - right.length)
    if (diff < bestDiff) {
      bestDiff = diff
      bestSplit = i
    }
  }
  return [
    words.slice(0, bestSplit + 1).join(' '),
    words.slice(bestSplit + 1).join(' '),
  ]
}

// Wrap description text to fit `maxChars` per line, capping at `maxLines`
// and adding an ellipsis to the final line if there's overflow.
function wrapText(text: string, maxChars: number, maxLines: number): string[] {
  const words = text.split(' ')
  const lines: string[] = []
  let cur = ''
  for (const w of words) {
    if (lines.length >= maxLines - 1 && `${cur} ${w}`.length > maxChars) {
      lines.push(`${`${cur} ${w}`.slice(0, maxChars - 1)}\u2026`)
      return lines
    }
    if (cur && `${cur} ${w}`.length > maxChars) {
      lines.push(cur)
      cur = w
    } else {
      cur = cur ? `${cur} ${w}` : w
    }
  }
  if (cur) lines.push(cur)
  return lines.slice(0, maxLines)
}

// Layout constants for the left content stack inside the 1200x657 card.
// Positions are computed so the (eyebrow + title + description) block is
// vertically centered around CENTER_Y. The 0.78 ASCENT_RATIO converts each
// line's visual top (its "box") into the baseline that SVG y= anchors to.
const LEFT_X = 81
const CENTER_Y = 328.5
const EYEBROW_H = 30
const TITLE_LINE_H = 89
const DESC_LINE_H = 49
const GAP_EYEBROW_TITLE = 32
const GAP_TITLE_DESC = 28
const ASCENT_RATIO = 0.78

function computeStackY(nTitle: number, nDesc: number) {
  const titleBlock = nTitle * TITLE_LINE_H
  const descBlock = nDesc > 0 ? GAP_TITLE_DESC + nDesc * DESC_LINE_H : 0
  const totalH = EYEBROW_H + GAP_EYEBROW_TITLE + titleBlock + descBlock
  const topY = CENTER_Y - totalH / 2

  const titleTop = topY + EYEBROW_H + GAP_EYEBROW_TITLE
  const firstTitleBaseline = titleTop + TITLE_LINE_H * ASCENT_RATIO
  const titleBaselines = Array.from(
    { length: nTitle },
    (_, i) => firstTitleBaseline + i * TITLE_LINE_H,
  )
  const titleBottom = titleTop + titleBlock

  const descBaselines = Array.from({ length: nDesc }, (_, i) => {
    const descTop = titleBottom + GAP_TITLE_DESC
    return descTop + DESC_LINE_H * ASCENT_RATIO + i * DESC_LINE_H
  })

  const eyebrowBaseline = topY + EYEBROW_H * ASCENT_RATIO

  return { eyebrowBaseline, titleBaselines, descBaselines }
}

function buildSvg(
  category: string | null,
  subcategory: string | null,
  title: string,
  description: string,
): string {
  let svg = TEMPLATE_SVG

  const titleLines = balanceLines(title, 99)
  const descLines = description ? wrapText(description, 41, 3) : []
  const { eyebrowBaseline, titleBaselines, descBaselines } = computeStackY(
    titleLines.length,
    descLines.length,
  )

  if (category) {
    const catUp = esc(category.toUpperCase())
    let eyebrowText = `<tspan x="${LEFT_X}" y="${eyebrowBaseline.toFixed(2)}">${catUp}</tspan>`
    if (subcategory) {
      const subUp = esc(subcategory.toUpperCase())
      const subX = LEFT_X + catUp.length * 16 + 48
      const chevX = LEFT_X + catUp.length * 16 + 18
      eyebrowText += `<tspan x="${chevX}" y="${(eyebrowBaseline - 1.77).toFixed(2)}">»</tspan>`
      eyebrowText += `<tspan x="${subX}" y="${eyebrowBaseline.toFixed(2)}">${subUp}</tspan>`
    }
    svg = svg.replace(
      /<text id="CATEGORY"[^>]*>[\s\S]*?<\/text>/,
      `<text id="CATEGORY" opacity="0.4" fill="white" xml:space="preserve" font-family="Geist Mono" font-size="28" font-weight="400" letter-spacing="0.01em">${eyebrowText}</text>`,
    )
  } else {
    svg = svg.replace(/<text id="CATEGORY"[^>]*>[\s\S]*?<\/text>/, '')
  }

  const titleTspans = titleLines
    .map(
      (line, i) =>
        `<tspan x="${LEFT_X}" y="${titleBaselines[i].toFixed(2)}">${esc(line)}</tspan>`,
    )
    .join('')
  svg = svg.replace(
    /<text id="Route title"[^>]*>[\s\S]*?<\/text>/,
    `<text id="Route title" fill="white" xml:space="preserve" font-family="Bodoni Moda" font-weight="500" font-size="99" letter-spacing="-0.02em">${titleTspans}</text>`,
  )

  if (descLines.length > 0) {
    const descTspans = descLines
      .map(
        (line, i) =>
          `<tspan x="${LEFT_X}" y="${descBaselines[i].toFixed(2)}">${esc(line)}</tspan>`,
      )
      .join('')
    svg = svg.replace(
      /<text id="Description[^>]*>[\s\S]*?<\/text>/,
      `<text opacity="0.6" fill="white" xml:space="preserve" font-family="Source Serif 4" font-weight="400" font-size="41" letter-spacing="0em">${descTspans}</text>`,
    )
  } else {
    svg = svg.replace(/<text id="Description[^>]*>[\s\S]*?<\/text>/, '')
  }

  return svg
}

// Map an mdx file path under pages/ to its public route, e.g.
// pages/extend/overlay.mdx -> /extend/overlay; pages/index.mdx -> /.
function pageRoute(mdxPath: string): string {
  const rel = relative(PAGES_DIR, mdxPath).replace(/\\/g, '/')
  const noExt = rel.replace(/\.mdx$/, '')
  if (noExt === 'index') return '/'
  return `/${noExt}`
}

// Walk pages/ recursively, collecting .mdx files.
function collectMdx(dir: string): string[] {
  const out: string[] = []
  for (const name of readdirSync(dir)) {
    const p = join(dir, name)
    const st = statSync(p)
    if (st.isDirectory()) {
      // Skip non-route subdirs that don't exist on the site (components etc.)
      if (name === 'components') continue
      out.push(...collectMdx(p))
    } else if (name.endsWith('.mdx')) {
      out.push(p)
    }
  }
  return out
}

function routeSlug(route: string): string {
  if (route === '/') return 'index'
  return route.replace(/^\//, '').replace(/\//g, '_')
}

async function main() {
  console.log('Initializing resvg-wasm...')
  // Resolve the wasm binary that ships in @resvg/resvg-wasm and feed it to
  // initWasm so we can rasterize without spawning a native subprocess.
  const wasmPath = join(DOCS_ROOT, 'node_modules', '@resvg', 'resvg-wasm', 'index_bg.wasm')
  await initWasm(readFileSync(wasmPath))

  const fonts = await loadAllFonts()
  console.log(`Loaded ${fonts.length} fonts`)

  const mdxFiles = collectMdx(PAGES_DIR)
  const generated: string[] = []

  for (const mdxPath of mdxFiles) {
    const route = pageRoute(mdxPath)
    const slug = routeSlug(route)
    const outPath = join(OUT_DIR, `${slug}.png`)

    let svg: string
    if (route === '/') {
      svg = HOME_SVG
    } else {
      const raw = readFileSync(mdxPath, 'utf-8')
      const { data } = matter(raw)
      const title = String(data.title ?? '').trim() || route
      const description = String(data.description ?? '').trim()
      const category = getCategoryForPath(route)
      const subcategory = getSubcategoryForPath(route)
      svg = buildSvg(category, subcategory, title, description)
    }

    const resvg = new Resvg(svg, {
      fitTo: { mode: 'width', value: 1200 },
      font: { fontBuffers: fonts, loadSystemFonts: false },
    })
    writeFileSync(outPath, resvg.render().asPng())
    generated.push(`${route} -> ${relative(DOCS_ROOT, outPath)}`)
  }

  // Default fallback used when Vocs's per-path ogImageUrl mapping doesn't
  // match — clone the homepage card so unknown / new routes still get an
  // on-brand image.
  writeFileSync(join(OUT_DIR, '_default.png'), readFileSync(join(OUT_DIR, 'index.png')))

  console.log(`Generated ${generated.length} OG cards:`)
  for (const line of generated) console.log(`  ${line}`)
}

main().catch((err) => {
  console.error(err)
  process.exit(1)
})
