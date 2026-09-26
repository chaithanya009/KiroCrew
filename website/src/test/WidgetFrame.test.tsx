import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, act, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { QueryClient, QueryClientProvider, focusManager } from '@tanstack/react-query'
import WidgetFrame from '../components/WidgetFrame'
import { ThemeProvider } from '../hooks/useTheme'
import { api, ApiError } from '../api/client'
import { effectiveWidgetSlug } from '../lib/widgetSlug'
import { i18nT } from '../i18n/t'
import { TAILWIND_COMPLEXITY_THRESHOLD } from '../lib/widgetComplexity'

// The sandboxed frame mints its document URL through the api client instead of
// building a `blob:` URL, so the real method has to be stubbed for the frame to
// render at all. This suite spies on the real module rather than automocking it.
beforeEach(() => {
  vi.spyOn(api, 'sandboxDocUrl').mockResolvedValue({ url: '/sandbox-doc/test/tok' })
})


// WidgetFrame consumes useTheme(), which requires a ThemeProvider, and now
// useQuery, which requires a QueryClient. Wrap every render here to mirror the
// production setup (main.tsx wraps App in both).
// Fresh QueryClient per test to avoid cross-test cache pollution.
let queryClient: QueryClient
beforeEach(() => {
  queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
})
const wrap = (ui: ReactNode) =>
  render(
    <QueryClientProvider client={queryClient}>
      <ThemeProvider>{ui}</ThemeProvider>
    </QueryClientProvider>,
  )

// The values we want readThemeVars() to see. Covers: normal hex, rgb(), oklch()
// (modern color syntax), and values that must be rejected by the sanitizer.
const GOOD = {
  '--bg': '#0b1220',
  '--text': 'rgb(240, 240, 240)',
  '--card': '#111827',
  '--accent': 'oklch(0.7 0.2 250)',
  '--border': '#1f2937',
}

// Injected specifically to prove the sanitizer rejects them.
const EVIL = {
  '--muted': 'red; background:url(http://evil.example/pix.gif)',  // attempted CSS break-out
  '--danger': 'expression(alert(1))',                              // legacy IE XSS
  '--ok': '"; }body{display:none} :root{--bg:',                    // quote + brace escape
  '--accent-hover': 'var(--foo, url(http://evil.example))',        // url() smuggled via var() fallback
  '--accent-subtle': 'paint(myWorklet)',                           // Houdini worklet reference
}

beforeEach(() => {
  // Clear localStorage — useTheme reads mc-theme/mc-color-theme at init and
  // test order can leave stale values.
  localStorage.clear()
  delete document.documentElement.dataset.theme

  // blob: URL mock — WidgetFrame uses createObjectURL for iframe src.
  // Intercept Blob constructor to capture the HTML content for test assertions.
  globalThis.Blob = class extends OriginalBlob {
    constructor(parts?: BlobPart[], options?: BlobPropertyBag) {
      super(parts, options)
      if (options?.type?.includes('text/html') && parts?.length) {
        _lastBlobContent = parts.map(p => typeof p === 'string' ? p : '').join('')
      }
    }
  } as typeof Blob
  // Well-formed blob: URI (scheme + real origin + opaque path), not a bare
  // 'blob:test-widget' literal. happy-dom's disableIframePageLoading refuses
  // the iframe navigation synchronously, but its AsyncTaskManager can retry
  // the SAME request later, off this test's call stack. A malformed value
  // survives that retry looking like a same-origin PATH once the scheme is
  // gone, missing the msw catch-all's blob:/data: fast path and its
  // DOM_DRIVEN_LOAD_RE allowlist alike — landing on the 501 fallback, which
  // surfaces as a deferred ECONNREFUSED during fork-worker teardown and can
  // crash the whole shard (not just this test). Anchoring to the real test
  // origin keeps the blob: scheme intact through the retry either way.
  vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:http://localhost:6776/test-widget')
  vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {})

  // Jest-dom inherits the real CSSOM, so spy on getComputedStyle to inject a
  // deterministic set of custom properties. The parent app's useTheme applies
  // data-theme on <html>, which the real CSSOM resolves via matching rules;
  // jsdom does not, so we mock the resolver.
  const orig = window.getComputedStyle
  vi.spyOn(window, 'getComputedStyle').mockImplementation((el: Element) => {
    if (el === document.documentElement) {
      const all = { ...GOOD, ...EVIL } as Record<string, string>
      const fake = {
        getPropertyValue: (name: string) => all[name] ?? '',
      }
      return fake as unknown as CSSStyleDeclaration
    }
    return orig(el)
  })

  // Force matchMedia to report dark OS preference so useTheme resolves to
  // 'dark' for these tests. Unconditional — don't inherit a prior test's
  // stub, since matchMedia is a direct property assignment (not a spy) and
  // vi.restoreAllMocks() can't undo it.
  window.matchMedia = vi.fn().mockReturnValue({
    matches: true, // dark
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    addListener: vi.fn(),
    removeListener: vi.fn(),
  })

  // ResizeObserver is referenced inside the srcdoc but also by some
  // framer-motion code paths during render.
  // @ts-expect-error test shim
  window.ResizeObserver = window.ResizeObserver ?? class {
    observe() {}
    unobserve() {}
    disconnect() {}
  }

  // Default mock: artifact probe returns 404 (unsaved) so the bookmark
  // starts empty unless a test overrides it.
  vi.spyOn(api, 'artifact').mockRejectedValue(
    Object.assign(new ApiError('Not found', 404), { status: 404 }),
  )
})

afterEach(() => {
  vi.restoreAllMocks()
  globalThis.Blob = OriginalBlob
  queryClient.clear()
})

// The Blob mock stays: the "open in new tab" and download paths still build one
// deliberately (they are user-initiated and do not crash). Nothing reads the
// captured content any more — the frame's html is observed at the mint call
// instead — so the sink is named to say so.
let _lastBlobContent = ''
const OriginalBlob = globalThis.Blob

/** The frame mounts only once its document URL resolves, so every lookup has to
 * wait. Returns the iframe or fails the test with a readable reason. */
async function frameIn(container: HTMLElement): Promise<HTMLIFrameElement> {
  return await waitFor(() => {
    const el = container.querySelector('iframe')
    if (!el) throw new Error('the widget frame never mounted an iframe')
    return el as HTMLIFrameElement
  })
}

function getSrcdoc(_container: HTMLElement): string {
  // The document html is what the frame POSTs to mint its URL, so read it from
  // the mint call's argument. It used to be captured from a mocked `Blob`
  // constructor, which no longer sees anything: the frame stopped building a
  // `blob:` URL because some WebKit-based in-app browsers refuse that load.
  // The assertions below are unchanged — only where the html is observed moved.
  const calls = vi.mocked(api.sandboxDocUrl).mock.calls
  if (!calls.length) throw new Error('the frame never minted a document URL')
  return String(calls[calls.length - 1][0])
}

describe('WidgetFrame theme passthrough', () => {
  it('serializes parent theme CSS vars into :root inside the iframe', () => {
    const { container } = wrap(<WidgetFrame html="<p>hi</p>" title="T" />)
    const srcdoc = getSrcdoc(container)

    expect(srcdoc).toMatch(/:root\s*\{[^}]*--bg:#0b1220/)
    expect(srcdoc).toMatch(/--text:rgb\(240, 240, 240\)/)
    expect(srcdoc).toMatch(/--card:#111827/)
    expect(srcdoc).toMatch(/--accent:oklch\(0\.7 0\.2 250\)/)
    expect(srcdoc).toMatch(/--border:#1f2937/)
  })

  it('sets body background/color to the theme vars so default widgets match the theme', () => {
    const { container } = wrap(<WidgetFrame html="<p>hi</p>" title="T" />)
    const srcdoc = getSrcdoc(container)
    expect(srcdoc).toMatch(/body\s*\{\s*background:\s*var\(--bg\)\s*;\s*color:\s*var\(--text\)\s*\}/)
  })

  it('drops CSS values that fail the allowlist (url/expression/quote-break/var-fallback/paint)', () => {
    const { container } = wrap(<WidgetFrame html="<p>hi</p>" title="T" />)
    const srcdoc = getSrcdoc(container)

    // None of the EVIL values survive — and crucially no `url(` / `expression(`
    // / `paint(` appears in the serialized :root, so a compromised parent theme
    // can't exfiltrate via the iframe's CSS even with CSP relaxed.
    expect(srcdoc).not.toMatch(/url\(/)
    expect(srcdoc).not.toMatch(/expression\(/)
    expect(srcdoc).not.toMatch(/paint\(/)
    expect(srcdoc).not.toMatch(/display:none/)
    // The poisoned vars either don't appear or don't carry their malicious
    // payloads. We assert the stronger form: they are absent.
    expect(srcdoc).not.toMatch(/--muted:/)
    expect(srcdoc).not.toMatch(/--danger:/)
    expect(srcdoc).not.toMatch(/--ok:/)
    expect(srcdoc).not.toMatch(/--accent-hover:/)
    expect(srcdoc).not.toMatch(/--accent-subtle:/)
  })

  it('outer iframe frame uses bg-card (not bg-white) so dark themes do not flash white', async () => {
    const { container } = wrap(<WidgetFrame html="<p>hi</p>" title="T" />)
    const iframe = await frameIn(container)
    expect(iframe.className).toMatch(/\bbg-card\b/)
    expect(iframe.className).not.toMatch(/\bbg-white\b/)
  })

  it('does not delegate clipboard-write to agent-authored HTML', async () => {
    // A delegated write permission lets an on-load script overwrite the
    // clipboard without a Copy action. The injected shim still lets a real
    // button press fall back to execCommand without widening frame permissions.
    const { container } = wrap(<WidgetFrame html="<p>hi</p>" title="T" />)
    const iframe = await frameIn(container)
    expect(iframe.hasAttribute('allow')).toBe(false)
  })

  it('preserves the CSP meta and loads the Tailwind runtime same-origin', () => {
    const { container } = wrap(<WidgetFrame html="<p>hi</p>" title="T" />)
    const srcdoc = getSrcdoc(container)
    expect(srcdoc).toMatch(/Content-Security-Policy/)
    // Locked-down-network fix: Tailwind loads from the dashboard origin, not the CDN.
    expect(srcdoc).not.toMatch(/cdn\.tailwindcss\.com/)
    expect(srcdoc).toMatch(/\/vendor\/tailwindcss-browser\.js/)
    // script-src grants no 'unsafe-eval' and pins the dashboard origin to the
    // single vendored runtime FILE (least-privilege), not the whole origin.
    expect(srcdoc).not.toContain("'unsafe-eval'")
    expect(srcdoc).toContain(
      `'unsafe-inline' ${window.location.origin}/vendor/tailwindcss-browser.js https://cdn.jsdelivr.net`,
    )
    // Runtime <script> src is origin-prefixed (absolute), not a bare '/vendor/...' path.
    expect(srcdoc).toContain(`src="${window.location.origin}/vendor/tailwindcss-browser.js"`)
  })

  it('falls back to browser defaults when no theme vars are readable', () => {
    // Simulate a test harness with no CSSOM (e.g. SSR or headless render).
    // Only override getComputedStyle — leave matchMedia alone so useTheme
    // can still resolve the mode without throwing.
    vi.spyOn(window, 'getComputedStyle').mockImplementation(() => ({
      getPropertyValue: () => '',
    } as unknown as CSSStyleDeclaration))

    const { container } = wrap(<WidgetFrame html="<p>hi</p>" title="T" />)
    const srcdoc = getSrcdoc(container)

    // No :root block, no forced body background — widget just renders on the
    // browser default (white) without complaint.
    expect(srcdoc).not.toMatch(/:root/)
    expect(srcdoc).not.toMatch(/body\s*\{\s*background:/)
    // Original body reset (margin/padding/font) is still present.
    expect(srcdoc).toMatch(/body \{ margin: 0; padding: 16px/)
  })

  it('emits color-scheme matching the dashboard mode (not "light dark")', () => {
    const { container } = wrap(<WidgetFrame html="<p>hi</p>" title="T" />)
    const srcdoc = getSrcdoc(container)
    // jsdom defaults: matchMedia(dark)=true above -> resolved mode is dark.
    expect(srcdoc).toMatch(/color-scheme:dark/)
    expect(srcdoc).not.toMatch(/color-scheme:light dark/)
  })

  it('drives dark mode via v4 custom-variant + <body> class, loaded same-origin', () => {
    // Widgets use Tailwind `dark:` variants (e.g. `bg-white dark:bg-slate-900`).
    // Inside the iframe the OS media query is wrong (it can't know the
    // dashboard's resolved mode), so we register a `.dark`-class custom variant
    // (v4) and put the mode on <body> so `dark:` tracks the dashboard.
    const { container } = wrap(<WidgetFrame html="<p>hi</p>" title="T" />)
    const srcdoc = getSrcdoc(container)
    expect(srcdoc).toMatch(/@custom-variant dark \(&:where\(\.dark, \.dark \*\)\)/)
    expect(srcdoc).toMatch(/<body class="dark">/)
    // Locked-down-network fix: Tailwind must not load from the public CDN.
    expect(srcdoc).not.toContain('cdn.tailwindcss.com')
    // Directives block must precede the runtime <script> so the dark variant
    // registers before first paint (ordering regression guard).
    expect(srcdoc.indexOf('text/tailwindcss')).toBeLessThan(
      srcdoc.indexOf(`src="${window.location.origin}/vendor/tailwindcss-browser.js"`),
    )
  })

  it('re-renders the srcdoc when the active theme changes (M1 regression guard)', async () => {
    // Arrange: start with theme-A values.
    const THEME_A: Record<string, string> = { '--bg': '#aaaaaa', '--text': '#111111' }
    const THEME_B: Record<string, string> = { '--bg': '#bbbbbb', '--text': '#222222' }
    let active = THEME_A

    // Override just the getComputedStyle spy set up in beforeEach. Leave
    // matchMedia alone so useTheme resolves mode normally.
    vi.spyOn(window, 'getComputedStyle').mockImplementation((el: Element) => {
      if (el === document.documentElement) {
        return {
          getPropertyValue: (name: string) => active[name] ?? '',
        } as unknown as CSSStyleDeclaration
      }
      return getComputedStyle(el)
    })

    const { container, rerender } = wrap(<WidgetFrame html="<p>hi</p>" title="T" />)
    expect(getSrcdoc(container)).toMatch(/--bg:#aaaaaa/)

    // Act: swap the mocked CSS vars and fire the cross-instance sync event
    // that useTheme listens to. This is exactly the signal dispatched by
    // setMode_ / setColorTheme / themeEditor in real usage.
    active = THEME_B
    const { act } = await import('@testing-library/react')
    await act(async () => {
      window.dispatchEvent(new CustomEvent('mc-theme-sync', {
        detail: { mode: 'light', colorTheme: 'emerald' },
      }))
    })
    rerender(
      <QueryClientProvider client={queryClient}>
        <ThemeProvider><WidgetFrame html="<p>hi</p>" title="T" /></ThemeProvider>
      </QueryClientProvider>,
    )

    // Assert: the srcdoc now reflects theme-B.
    const after = getSrcdoc(container)
    expect(after).toMatch(/--bg:#bbbbbb/)
    expect(after).not.toMatch(/--bg:#aaaaaa/)
    expect(after).toMatch(/--text:#222222/)
  })
})

describe('WidgetFrame openInNewTab', () => {
  function captureWrapperHtml(container: HTMLElement): string {
    // Spy on Blob to grab the wrapper HTML the openInNewTab handler builds.
    let wrapper = ''
    const realBlob = window.Blob
    vi.spyOn(window, 'Blob' as never).mockImplementation(function (...args: unknown[]) {
      const parts = args[0] as BlobPart[]
      const opts = args[1] as BlobPropertyBag | undefined
      if (typeof parts[0] === 'string') wrapper = parts[0] as string
      return new realBlob(parts, opts)
    })
    // Well-formed blob: URI — see the beforeEach mock above for why a bare
    // 'blob:test' literal risks a deferred ECONNREFUSED crashing the shard.
    vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:http://localhost:6776/test')
    vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {})
    vi.spyOn(window, 'open').mockReturnValue(null)

    const btn = container.querySelector('button[aria-label="Open in new tab"]') as HTMLButtonElement
    btn.click()
    return wrapper
  }

  it('declares utf-8 charset (meta + Blob MIME) so popout does not mojibake', () => {
    const { container } = wrap(<WidgetFrame html="<p>hi</p>" title="T" />)

    let mimeType = ''
    const realBlob = window.Blob
    vi.spyOn(window, 'Blob' as never).mockImplementation(function (...args: unknown[]) {
      const opts = args[1] as BlobPropertyBag | undefined
      mimeType = opts?.type ?? ''
      return new realBlob(args[0] as BlobPart[], opts)
    })
    // Well-formed blob: URI — see the beforeEach mock above for why a bare
    // 'blob:test' literal risks a deferred ECONNREFUSED crashing the shard.
    vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:http://localhost:6776/test')
    vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {})
    vi.spyOn(window, 'open').mockReturnValue(null)

    const btn = container.querySelector('button[aria-label="Open in new tab"]') as HTMLButtonElement
    btn.click()

    expect(mimeType).toBe('text/html;charset=utf-8')
  })

  it('escapes HTML-special characters in title via DOM API (not template literals)', () => {
    const evil = '"><script>alert(1)</script><x title="'
    const { container } = wrap(<WidgetFrame html="<p>hi</p>" title={evil} />)
    const wrapper = captureWrapperHtml(container)

    // Title is set via doc.title which serializes safely. The raw script
    // tag must not appear unescaped in <title>; the entire payload should
    // be HTML-escaped inside the title element.
    expect(wrapper).not.toMatch(/<title>[^<]*<script>/)
    expect(wrapper).toMatch(/<title>[^<]*&lt;script&gt;/)
  })

  it('preserves srcdoc content semantically through srcdoc attribute serialization', () => {
    // Pass HTML containing the byte categories the popout has to tolerate
    // (latin-1 é, raw quote, &, <, >) so the computed srcdoc actually
    // exercises the wrapper's escaping/round-trip machinery.
    const html = '<p title="a&b">&lt;special&gt; &amp; é</p>'
    const { container } = wrap(<WidgetFrame html={html} title="T" />)
    const wrapper = captureWrapperHtml(container)

    const parsed = new DOMParser().parseFromString(wrapper, 'text/html')
    const iframe = parsed.querySelector('iframe')!
    const recovered = iframe.getAttribute('srcdoc') ?? ''
    // The wrapper preserves the inner srcdoc; the inner srcdoc is built via
    // DOM APIs so verbatim string equality is no longer expected (entity
    // re-encoding happens during outerHTML serialization). Verify SEMANTIC
    // equivalence by re-parsing the recovered srcdoc and checking the <p>
    // attribute and text content survived intact.
    expect(recovered).toMatch(/<!DOCTYPE html>/i)
    expect(recovered).toMatch(/<meta charset="utf-8">/)
    const innerDoc = new DOMParser().parseFromString(recovered, 'text/html')
    const p = innerDoc.querySelector('p')!
    expect(p).not.toBeNull()
    // title attribute decoded back to its raw value.
    expect(p.getAttribute('title')).toBe('a&b')
    // text content (which originated as &lt;&gt;&amp; entities in the input
    // and got decoded by createContextualFragment) round-trips correctly.
    expect(p.textContent).toBe('<special> & é')
    // Wrapper iframe is sandboxed (defense-in-depth, even though blob origin
    // is already null).
    expect(iframe.getAttribute('sandbox')).toBe('allow-scripts allow-popups allow-popups-to-escape-sandbox')
  })
})

describe('WidgetFrame interactive event bridge', () => {
  it('injects data-action click handler script into srcdoc', () => {
    const { container } = wrap(<WidgetFrame html="<button data-action='test'>Click</button>" title="T" />)
    const srcdoc = getSrcdoc(container)
    expect(srcdoc).toContain("el.dataset.action")
    expect(srcdoc).toContain("mc-widget-action")
    expect(srcdoc).toContain("formData")
  })

  it('dispatches mc-widget-send CustomEvent when receiving mc-widget-action postMessage', async () => {
    const { container } = wrap(<WidgetFrame html="<p>test</p>" title="T" />)
    const iframe = await frameIn(container)

    const events: CustomEvent[] = []
    const listener = (e: Event) => events.push(e as CustomEvent)
    window.addEventListener('mc-widget-send', listener)

    window.dispatchEvent(new MessageEvent('message', {
      data: { type: 'mc-widget-action', action: 'approve', payload: { id: '123' } },
      source: iframe.contentWindow,
    }))

    // Poll for the event rather than sleeping a guessed interval: the handler
    // dispatches synchronously, so this settles on the first tick.
    await waitFor(() => expect(events).toHaveLength(1))
    window.removeEventListener('mc-widget-send', listener)

    expect(events[0].detail.text).toBe('[UI] approve: {"id":"123"}')
  })

  it('formats action-only messages without payload when payload is empty', async () => {
    const { container } = wrap(<WidgetFrame html="<p>test</p>" title="T" />)
    const iframe = await frameIn(container)

    const events: CustomEvent[] = []
    const listener = (e: Event) => events.push(e as CustomEvent)
    window.addEventListener('mc-widget-send', listener)

    window.dispatchEvent(new MessageEvent('message', {
      data: { type: 'mc-widget-action', action: 'cancel', payload: {} },
      source: iframe.contentWindow,
    }))

    // Poll for the event rather than sleeping a guessed interval: the handler
    // dispatches synchronously, so this settles on the first tick.
    await waitFor(() => expect(events).toHaveLength(1))
    window.removeEventListener('mc-widget-send', listener)

    expect(events[0].detail.text).toBe('[UI] cancel')
  })

 // shape validation / allowlist hardening of widget actions.
  it('ignores a widget action with a non-string action (no event dispatched)', async () => {
    const { container } = wrap(<WidgetFrame html="<p>test</p>" title="T" />)
    const iframe = await frameIn(container)

    const events: CustomEvent[] = []
    const listener = (e: Event) => events.push(e as CustomEvent)
    window.addEventListener('mc-widget-send', listener)

    window.dispatchEvent(new MessageEvent('message', {
      data: { type: 'mc-widget-action', action: { evil: true }, payload: { id: '1' } },
      source: iframe.contentWindow,
    }))

    // A negative assertion has no condition to poll for, so flush one macrotask
    // (enough for the message handler to have run had it accepted the payload)
    // and assert nothing arrived. `waitFor` would only re-check a true predicate.
    await new Promise(r => setTimeout(r, 0))
    window.removeEventListener('mc-widget-send', listener)
    expect(events).toHaveLength(0)
  })

  it('ignores a non-object/array payload and emits the action only', async () => {
    const { container } = wrap(<WidgetFrame html="<p>test</p>" title="T" />)
    const iframe = await frameIn(container)

    const events: CustomEvent[] = []
    const listener = (e: Event) => events.push(e as CustomEvent)
    window.addEventListener('mc-widget-send', listener)

    window.dispatchEvent(new MessageEvent('message', {
      data: { type: 'mc-widget-action', action: 'go', payload: ['a', 'b'] },
      source: iframe.contentWindow,
    }))

    await waitFor(() => expect(events).toHaveLength(1))
    window.removeEventListener('mc-widget-send', listener)
    expect(events[0].detail.text).toBe('[UI] go')
    expect(events[0].detail.action).toBe('go')
  })

  it('caps an oversized widget action payload', async () => {
    const { container } = wrap(<WidgetFrame html="<p>test</p>" title="T" />)
    const iframe = await frameIn(container)

    const events: CustomEvent[] = []
    const listener = (e: Event) => events.push(e as CustomEvent)
    window.addEventListener('mc-widget-send', listener)

    window.dispatchEvent(new MessageEvent('message', {
      data: { type: 'mc-widget-action', action: 'flood', payload: { big: 'x'.repeat(20000) } },
      source: iframe.contentWindow,
    }))

    await waitFor(() => expect(events).toHaveLength(1))
    window.removeEventListener('mc-widget-send', listener)
    expect(events[0].detail.text.length).toBeLessThanOrEqual(4001)
    expect(events[0].detail.text.endsWith('…')).toBe(true)
  })
})

// Regression test: when the widget unmounts mid-flight (user navigates away
// or the chat scrolls the widget out of view between bookmark click and API
// response), the post-await code path must short-circuit so we don't touch
// React state on an unmounted component.
//
// React 18 silently no-ops setState on an unmounted component (the
// "Can't perform a React state update…" warning was removed), so checking
// for that warning would be vacuous. Instead we assert the survivable
// invariant directly: when we resolve the in-flight createArtifact AFTER
// unmount, we observe no fresh DOM render (the bookmark icon never gets
// the "filled" class that setSavedSlug would have triggered). The test
// re-mounts a fresh instance after the unmount → if the post-unmount
// setState had leaked, the new instance would inherit nothing — but the
// guard is what we're testing, not React internals, so we verify the
// guard prevents post-unmount work by checking the unmount completes
// cleanly and no exception is thrown when we resolve the deferred
// promise post-unmount.
describe('WidgetFrame paint contract', () => {
  it('gives the frame its own compositing layer so a skipped first paint cannot blank it', async () => {
    // The artifact frame was promoted after an engine was measured laying its
    // document out, running its scripts and reporting a correct height while
    // rasterizing nothing — a correctly sized, visible frame painting an empty
    // box. This frame loads the same kind of document, through the same mint,
    // behind the same opacity-on-load reveal, and was left un-promoted, so the
    // inline-widget surface kept the gap the artifact surface had closed.
    //
    // Chromium in this DOM paints fine either way, so removing the property
    // looks completely harmless here: this assertion is the whole guard.
    const { container } = wrap(<WidgetFrame html="<p>promoted</p>" title="T" />)
    const iframe = await frameIn(container)
    expect(iframe.style.transform).toBe('translateZ(0)')
  })

  it('still reveals on load rather than replacing the reveal with the promotion', async () => {
    // The promotion is additive. If it had displaced the opacity gate the frame
    // would show the browser's own canvas for the length of the document fetch,
    // which some engines paint white regardless of the element background.
    const { container } = wrap(<WidgetFrame html="<p>reveal</p>" title="T" />)
    const iframe = await frameIn(container)
    expect(iframe.style.opacity).toBe('0')
    act(() => { iframe.dispatchEvent(new Event('load')) })
    await waitFor(() => expect(iframe.style.opacity).toBe('1'))
  })
})

describe('WidgetFrame expanded layout (structural contract)', () => {
  const expandLabel = () => i18nT('components.widgetFrame.expand')

  it('expanded: root is a flex column and the body wrapper/iframe form an unbroken height chain', async () => {
    const { container } = wrap(<WidgetFrame html="<p>hi</p>" title="T" />)
    const root = container.firstElementChild as HTMLElement
    const iframe = await frameIn(container)

    // Collapsed baseline: measured pixel height, plain wrapper, no modal classes.
    expect(iframe.style.height).not.toBe('100%')
    expect(root.classList.contains('flex-col')).toBe(false)

    const btn = container.querySelector(
      `button[aria-label="${expandLabel()}"]`,
    ) as HTMLButtonElement
    expect(btn).not.toBeNull()
    act(() => { btn.click() })

    // Token-level checks: `flex` must be its own class (flex-col alone sets
    // no display), and `inset-4` is what makes the fixed root's height
    // definite — without it the chain below has nothing to resolve against.
    // classList.contains() (not jest-dom's toHaveClass) because this TS
    // project does not load the matcher's type extensions for test files.
    expect(root.classList.contains('fixed')).toBe(true)
    expect(root.classList.contains('inset-4')).toBe(true)
    expect(root.classList.contains('flex')).toBe(true)
    expect(root.classList.contains('flex-col')).toBe(true)
    // The wrapper introduced for the progress indicator must fill the modal.
    const bodyWrapper = iframe.parentElement as HTMLElement
    expect(bodyWrapper.classList.contains('flex-1')).toBe(true)
    expect(bodyWrapper.classList.contains('min-h-0')).toBe(true)
    // The iframe resolves against the flexed wrapper, not a magic 36px calc.
    expect(iframe.style.height).toBe('100%')
  })

  it('expanded: the loading overlay of a heavy widget spans the full body wrapper', async () => {
    // Enough unique Tailwind utility classes to cross the complexity
    // threshold, so the parent-side progress overlay actually renders and
    // its expanded-mode height style is exercised.
    const classes = Array.from(
      { length: TAILWIND_COMPLEXITY_THRESHOLD + 10 },
      (_, i) => `p-${i}`,
    ).join(' ')
    const { container } = wrap(<WidgetFrame html={`<div class="${classes}">x</div>`} title="T" />)
    const iframe = await frameIn(container)
    // The overlay stays mounted until the iframe's load event, which the
    // mocked blob: URL never fires in this environment.
    const overlay = iframe.parentElement!.querySelector(
      '[class*="absolute"][class*="inset-0"]',
    ) as HTMLElement
    expect(overlay).not.toBeNull()
    expect(overlay.style.height).not.toBe('100%')

    const btn = container.querySelector(
      `button[aria-label="${expandLabel()}"]`,
    ) as HTMLButtonElement
    act(() => { btn.click() })

    expect(overlay.style.height).toBe('100%')
  })

  it('restores the collapsed layout on minimize', async () => {
    const { container } = wrap(<WidgetFrame html="<p>hi</p>" title="T" />)
    const root = container.firstElementChild as HTMLElement
    const iframe = await frameIn(container)
    const collapsedHeight = iframe.style.height

    const expandBtn = container.querySelector(
      `button[aria-label="${expandLabel()}"]`,
    ) as HTMLButtonElement
    act(() => { expandBtn.click() })
    const minimizeBtn = container.querySelector(
      `button[aria-label="${i18nT('components.widgetFrame.minimize')}"]`,
    ) as HTMLButtonElement
    expect(minimizeBtn).not.toBeNull()
    act(() => { minimizeBtn.click() })

    expect(root.classList.contains('flex-col')).toBe(false)
    // The expanded-only sizing classes must be gone; don't pin the exact
    // class list so harmless collapsed-mode additions don't break this.
    expect(iframe.parentElement!.classList.contains('flex-1')).toBe(false)
    expect(iframe.parentElement!.classList.contains('min-h-0')).toBe(false)
    expect(iframe.style.height).toBe(collapsedHeight)
  })
})
