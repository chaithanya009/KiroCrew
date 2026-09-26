import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'
import type { ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import WidgetFrame, {
  staggeredBuildWait,
  MAX_STAGGER_SLOTS,
  MAX_WIDGET_BUILD_WAIT_MS,
  PROGRAMMATIC_BUILD_DELAY_MS,
} from './WidgetFrame'
import { ThemeProvider } from '../hooks/useTheme'
import { api } from '../api/client'
import { i18nT } from '../i18n/t'

let queryClient: QueryClient

/** Mirrors the module-private constant in WidgetFrame.tsx. */
const HEIGHT_SHRINK_DEBOUNCE_MS = 250

const wrap = (ui: ReactNode) =>
  render(
    <QueryClientProvider client={queryClient}>
      <ThemeProvider>{ui}</ThemeProvider>
    </QueryClientProvider>,
  )

beforeEach(() => {
  queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  localStorage.clear()
  vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:zzq-widget')
  // The frame mints a gateway document instead of building a blob for itself.
  vi.spyOn(api, 'sandboxDocUrl').mockResolvedValue({ url: '/sandbox-doc/test/tok' })
  vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {})
})

afterEach(() => {
  vi.restoreAllMocks()
  queryClient.clear()
})

/** Let the minted document URL resolve so the frame mounts.
 *
 * The frame's src now comes from a gateway round trip rather than a `blob:` URL
 * built inline, so the iframe is absent for one microtask after render. This
 * flushes that promise and applies the resulting React update.
 *
 * Deliberately NOT `waitFor`: several tests here run on fake timers, where
 * waitFor's own polling never advances and the wait never settles. A microtask
 * flush needs no clock. */
async function settleMint(): Promise<void> {
  await act(async () => {})
}

describe('staggeredBuildWait', () => {
  it('adds one stagger step per slot and then plateaus at the cap', () => {
    const base = PROGRAMMATIC_BUILD_DELAY_MS
    expect(staggeredBuildWait(base, 0)).toBe(base)
    expect(staggeredBuildWait(base, 1)).toBeGreaterThan(base)
    expect(staggeredBuildWait(base, MAX_STAGGER_SLOTS)).toBe(MAX_WIDGET_BUILD_WAIT_MS)
    // Beyond the cap the wait must not keep growing, or a late-slot widget
    // rebuilds after convergence has already settled.
    expect(staggeredBuildWait(base, MAX_STAGGER_SLOTS + 40)).toBe(MAX_WIDGET_BUILD_WAIT_MS)
  })
})

describe('WidgetFrame toolbar actions', () => {
  it('downloads the widget as a sanitized .html filename', () => {
    const click = vi.fn()
    const create = document.createElement.bind(document)
    vi.spyOn(document, 'createElement').mockImplementation((tag: string) => {
      const el = create(tag)
      if (tag === 'a') vi.spyOn(el as HTMLAnchorElement, 'click').mockImplementation(click)
      return el
    })

    wrap(<WidgetFrame html="<p>zzq</p>" title={'zzq/report:1*'} />)
    fireEvent.click(
      screen.getByRole('button', { name: i18nT('components.widgetFrame.download_as_html') }),
    )

    expect(click).toHaveBeenCalledTimes(1)
    expect(URL.createObjectURL).toHaveBeenCalled()
    // The anchor is removed again, so nothing is left in the document body.
    expect(document.querySelector('a[download]')).toBeNull()
  })

  it('opens a standalone wrapper document in a new tab', () => {
    const open = vi.spyOn(window, 'open').mockReturnValue(null)
    wrap(<WidgetFrame html="<p>zzq</p>" title="zzq widget" />)

    fireEvent.click(
      screen.getByRole('button', { name: i18nT('components.widgetFrame.open_in_new_tab') }),
    )
    expect(open).toHaveBeenCalledWith('blob:zzq-widget', '_blank')
  })

  it('expands, then collapses again from the backdrop', () => {
    const { container } = wrap(<WidgetFrame html="<p>zzq</p>" title="zzq widget" />)

    fireEvent.click(screen.getByRole('button', { name: i18nT('components.widgetFrame.expand') }))
    const backdrop = container.querySelector('.fixed.inset-0')!
    expect(backdrop).toBeTruthy()
    expect(
      screen.getByRole('button', { name: i18nT('components.widgetFrame.minimize') }),
    ).toBeInTheDocument()

    fireEvent.click(backdrop)
    expect(container.querySelector('.fixed.inset-0')).toBeNull()
    expect(screen.getByRole('button', { name: i18nT('components.widgetFrame.expand') }))
      .toBeInTheDocument()
  })

  it('grows immediately, defers a shrink, and ignores a repeat of the same height', async () => {
    vi.useFakeTimers()
    try {
      const { container } = wrap(<WidgetFrame html="<p>zzq</p>" title="zzq widget" />)
      await settleMint()
      const iframe = container.querySelector('iframe') as HTMLIFrameElement
      const post = (height: number) => {
        act(() => {
          window.dispatchEvent(
            new MessageEvent('message', {
              data: { type: 'mc-widget-height', height },
              source: iframe.contentWindow,
            }),
          )
        })
      }

      post(640)
      expect(iframe.style.height).toBe('640px')

      // A repeat reading is a no-op, and a shrink waits for the debounce.
      post(640)
      expect(iframe.style.height).toBe('640px')
      post(300)
      expect(iframe.style.height).toBe('640px')
      act(() => { vi.advanceTimersByTime(HEIGHT_SHRINK_DEBOUNCE_MS) })
      expect(iframe.style.height).toBe('300px')

      // Below MIN_HEIGHT the report is clamped rather than honoured.
      post(4)
      act(() => { vi.advanceTimersByTime(HEIGHT_SHRINK_DEBOUNCE_MS) })
      expect(iframe.style.height).toBe('80px')

      // Let the batched cache write land, so the module-level persist timer is
      // not left armed for the rest of the file.
      act(() => { vi.advanceTimersByTime(1000) })
    } finally {
      vi.useRealTimers()
    }
  })

  it('ignores a height message from a window that is not its own iframe', async () => {
    const { container } = wrap(<WidgetFrame html="<p>zzq</p>" title="zzq widget" />)
    await settleMint()
    const iframe = container.querySelector('iframe') as HTMLIFrameElement
    const before = iframe.style.height
    act(() => {
      window.dispatchEvent(
        new MessageEvent('message', {
          data: { type: 'mc-widget-height', height: 999 },
          source: window,
        }),
      )
    })
    expect(iframe.style.height).toBe(before)
  })

  it('revokes both action blob URLs once their grace window elapses', () => {
    vi.useFakeTimers()
    try {
      vi.spyOn(window, 'open').mockReturnValue(null)
      wrap(<WidgetFrame html="<p>zzq</p>" title="zzq widget" />)
      fireEvent.click(
        screen.getByRole('button', { name: i18nT('components.widgetFrame.open_in_new_tab') }),
      )
      fireEvent.click(
        screen.getByRole('button', { name: i18nT('components.widgetFrame.download_as_html') }),
      )
      expect(URL.revokeObjectURL).not.toHaveBeenCalledWith('blob:zzq-widget')
      act(() => { vi.advanceTimersByTime(60_000) })
      expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:zzq-widget')
    } finally {
      vi.useRealTimers()
    }
  })

  it('persists the height cache once the write debounce elapses', async () => {
    vi.useFakeTimers()
    try {
      const { container } = wrap(<WidgetFrame html="<p>zzq-persist</p>" title="zzq widget" />)
      await settleMint()
      const iframe = container.querySelector('iframe') as HTMLIFrameElement
      act(() => {
        window.dispatchEvent(new MessageEvent('message', {
          data: { type: 'mc-widget-height', height: 512 },
          source: iframe.contentWindow,
        }))
      })
      expect(localStorage.getItem('mc-widget-heights')).toBeNull()
      act(() => { vi.advanceTimersByTime(1000) })
      expect(localStorage.getItem('mc-widget-heights')).toContain('512')
    } finally {
      vi.useRealTimers()
    }
  })

  it('reserves the median of already-measured heights for an unseen widget', async () => {
    const { container, unmount } = wrap(
      <WidgetFrame html="<p>zzq-measured</p>" title="zzq widget" />,
    )
    await settleMint()
    const iframe = container.querySelector('iframe') as HTMLIFrameElement
    act(() => {
      window.dispatchEvent(new MessageEvent('message', {
        data: { type: 'mc-widget-height', height: 456 },
        source: iframe.contentWindow,
      }))
    })
    unmount()

    // Different content ⇒ a cache miss, so the reserve comes from the median of
    // what has been measured rather than the fixed 200px default.
    const second = wrap(<WidgetFrame html="<p>zzq-unseen</p>" title="zzq widget" />)
    await settleMint()
    const reserved = (second.container.querySelector('iframe') as HTMLIFrameElement).style.height
    expect(reserved).not.toBe('200px')
    expect(Number.parseInt(reserved, 10)).toBeGreaterThanOrEqual(80)
  })
})

describe('WidgetFrame reveal', () => {
  it('renders a reserved skeleton until the observer reports it near', async () => {
    const observed: Array<(entries: { isIntersecting: boolean }[]) => void> = []
    const original = globalThis.IntersectionObserver
    class FakeIO {
      constructor(cb: (entries: { isIntersecting: boolean }[]) => void) { observed.push(cb) }
      observe() {}
      disconnect() {}
    }
    globalThis.IntersectionObserver = FakeIO as unknown as typeof IntersectionObserver
    try {
      const { container } = wrap(<WidgetFrame html="<p>zzq</p>" title="zzq-skeleton" />)
      expect(container.querySelector('iframe')).toBeNull()
      expect(screen.getByText('zzq-skeleton')).toBeInTheDocument()
      // An entry that is not intersecting must not reveal it.
      // Absent here means genuinely not revealed, not merely a pending mint: an
      // unrevealed frame is passed no html at all, so no mint is even started.
      act(() => { observed[0]([{ isIntersecting: false }]) })
      expect(container.querySelector('iframe')).toBeNull()
      act(() => { observed[0]([{ isIntersecting: true }]) })
      await settleMint()
      expect(container.querySelector('iframe')).not.toBeNull()
    } finally {
      globalThis.IntersectionObserver = original
    }
  })

  it('reveals eagerly where IntersectionObserver does not exist', async () => {
    const original = globalThis.IntersectionObserver
    // @ts-expect-error — deliberately modelling an environment without the API.
    delete globalThis.IntersectionObserver
    try {
      const { container } = wrap(<WidgetFrame html="<p>zzq</p>" title="zzq widget" />)
      await settleMint()
      expect(container.querySelector('iframe')).not.toBeNull()
    } finally {
      globalThis.IntersectionObserver = original
    }
  })

  it('staggers the build behind a programmatic scroll jump', async () => {
    const original = globalThis.IntersectionObserver
    // @ts-expect-error — force the eager `near` path so only the jump delay gates.
    delete globalThis.IntersectionObserver
    vi.useFakeTimers()
    // A tiny fixed clock: `lastProgrammaticScrollAt` is module state, so a real
    // timestamp here would keep every later test inside the jump window.
    const now = vi.spyOn(Date, 'now').mockReturnValue(1_000)
    try {
      // The module-level listener records the jump time; the build then waits.
      act(() => { window.dispatchEvent(new Event('mc-chat-scroll-jump')) })
      const { container } = wrap(<WidgetFrame html="<p>zzq-jump</p>" title="zzq-jump" />)
      expect(container.querySelector('iframe')).toBeNull()
      act(() => { vi.advanceTimersByTime(MAX_WIDGET_BUILD_WAIT_MS) })
      await settleMint()
      expect(container.querySelector('iframe')).not.toBeNull()
    } finally {
      now.mockRestore()
      vi.useRealTimers()
      globalThis.IntersectionObserver = original
    }
  })

  it('fades the iframe in on load', async () => {
    const { container } = wrap(<WidgetFrame html="<p>zzq</p>" title="zzq widget" />)
    await settleMint()
    const iframe = container.querySelector('iframe') as HTMLIFrameElement
    expect(iframe.style.opacity).toBe('0')
    fireEvent.load(iframe)
    expect(iframe.style.opacity).toBe('1')
  })
})

describe('WidgetFrame widget actions', () => {
  // Waits for the frame itself, so each caller keeps reading as one step.
  const post = async (container: HTMLElement, data: unknown) => {
    await settleMint()
    const iframe = container.querySelector('iframe') as HTMLIFrameElement
    act(() => {
      window.dispatchEvent(new MessageEvent('message', {
        data, source: iframe.contentWindow,
      }))
    })
  }

  let sent: CustomEvent[]
  const listener = (e: Event) => { sent.push(e as CustomEvent) }

  beforeEach(() => {
    sent = []
    window.addEventListener('mc-widget-send', listener)
  })
  afterEach(() => { window.removeEventListener('mc-widget-send', listener) })

  it('prefills the composer with the action and its payload', async () => {
    const { container } = wrap(<WidgetFrame html="<p>zzq</p>" title="zzq widget" />)
    await post(container, { type: 'mc-widget-action', action: 'zzq-act', payload: { a: 1 } })
    expect(sent).toHaveLength(1)
    expect(sent[0].detail).toEqual({ text: '[UI] zzq-act: {"a":1}', action: 'zzq-act' })
  })

  it('omits the payload segment when there is nothing to carry', async () => {
    const { container } = wrap(<WidgetFrame html="<p>zzq</p>" title="zzq widget" />)
    await post(container, { type: 'mc-widget-action', action: 'zzq-act', payload: [1, 2] })
    expect(sent[0].detail.text).toBe('[UI] zzq-act')
  })

  it('refuses an action whose name is not a string', async () => {
    const { container } = wrap(<WidgetFrame html="<p>zzq</p>" title="zzq widget" />)
    await post(container, { type: 'mc-widget-action', action: { evil: true } })
    await post(container, { type: 'mc-widget-action', action: '' })
    expect(sent).toHaveLength(0)
  })

  it('caps an oversized payload so a widget cannot stuff the composer', async () => {
    const { container } = wrap(<WidgetFrame html="<p>zzq</p>" title="zzq widget" />)
    await post(container, {
      type: 'mc-widget-action',
      action: 'zzq-act',
      payload: { blob: 'z'.repeat(9000) },
    })
    expect(sent[0].detail.text).toHaveLength(4001)
    expect(sent[0].detail.text.endsWith('…')).toBe(true)
  })

  it('truncates an over-long action name to 64 characters', async () => {
    const { container } = wrap(<WidgetFrame html="<p>zzq</p>" title="zzq widget" />)
    await post(container, { type: 'mc-widget-action', action: 'z'.repeat(200) })
    expect(sent[0].detail.action).toHaveLength(64)
  })
})
