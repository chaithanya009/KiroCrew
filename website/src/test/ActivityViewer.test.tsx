import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

// jsdom polyfill: SegmentedControl uses ResizeObserver
if (typeof globalThis.ResizeObserver === 'undefined') {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver
}
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { configureStore } from '@reduxjs/toolkit'
import chatReducer from '../store/chatSlice'
import { openActivityToTab, sseSubagentQueued } from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import { sseSlots } from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'

vi.mock('../api/client', () => ({
  api: {
    workflowRuns: vi.fn().mockResolvedValue({ runs: [] }),
    browseFiles: vi.fn().mockResolvedValue({ path: '/projects/foo', parent: '/', dirs: [], files: [] }),
    pullRequestSource: vi.fn().mockImplementation(() => new Promise(() => {})),
    fileDiff: vi.fn().mockResolvedValue({ diff: '' }),
    // Artifacts tab: real session-scoped artifacts + the virtual file-backed docs.
    artifacts: vi.fn().mockResolvedValue({ artifacts: [] }),
    artifactSessionDocs: vi.fn().mockResolvedValue({ docs: [] }),
    materializeArtifact: vi.fn().mockResolvedValue({}),
    setArtifactPinned: vi.fn().mockResolvedValue({}),
    // Files tab: add-to-library on a file row.
    createArtifact: vi.fn().mockResolvedValue({ slug: 'new-slug', version: 1 }),
  },
}))

// MarkdownPanel pulls in Pierre + a large renderer tree; stub it so the
// Files-tab inline-preview test stays focused on the list↔file swap behavior.
// forwardRef + requestClose mirror the real imperative handle so the preview's
// "Back to files" button (which routes through the panel's guarded close) works
// against the stub without a "function components cannot be given refs" warning.
vi.mock('../components/MarkdownPanel', async () => {
  const { forwardRef, useImperativeHandle } = await import('react')
  return {
    default: forwardRef<{ requestClose: () => void }, { filePath: string; content: string; onClose: () => void; onSave: (p: string, c: string) => Promise<void>; onContentChange: (c: string) => void }>(
      ({ filePath, content, onClose, onSave, onContentChange }, ref) => {
        useImperativeHandle(ref, () => ({ requestClose: onClose }), [onClose])
        return (
          <div data-testid="md-panel">
            <span>{filePath}::{content}</span>
            <button data-testid="md-save" onClick={() => onSave(filePath, 'SAVED')}>save</button>
            <button data-testid="md-edit" onClick={() => onContentChange('EDITED')}>edit</button>
          </div>
        )
      },
    ),
  }
})

import ActivityViewer from '../pages/chat/ActivityViewer'
import { api } from '../api/client'
import { __resetPanelTabs } from '../hooks/usePanelTabs'

function wrapper({ children }: { children: React.ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const store = configureStore({
    reducer: { chat: chatReducer, dashboard: dashboardReducer, notifications: notificationsReducer },
  })
  return (
    <Provider store={store}>
      <QueryClientProvider client={qc}>{children}</QueryClientProvider>
    </Provider>
  )
}

/* ── Section-header helpers ───────────────────────────────────────────────────
 * Both list tabs render their group headers through PanelSectionHeader, which
 * keeps the count as a SIBLING node of the label rather than punctuation inside
 * it. So `findByText('Artifact library (1)')` matches nothing — read the pair
 * through these helpers instead. */

/** Every section header in the tree, as `[label, count]`. `count` is null when
 *  the header was rendered without one (children[1] is then the hairline rule). */
function sectionHeaders(): (string | null)[][] {
  return screen.getAllByTestId('panel-section-header').map(h => [
    h.children[0].textContent,
    h.children[1]?.classList.contains('h-px') ? null : h.children[1]?.textContent ?? null,
  ])
}

/** Waits for a section header carrying this label and count to appear. */
async function findSection(label: string, count: number) {
  await waitFor(() => {
    expect(sectionHeaders()).toContainEqual([label, String(count)])
  })
}

describe('ActivityViewer', () => {
  const baseProps = {
    subagents: {},
    toolLog: [],
    open: true,
    onToggle: vi.fn(),
    slot: 'test-slot',
  }

  // useSortableTable persists the chosen sort to localStorage keyed by tableId,
  // so clear it between tests to keep the file-browser sort tests independent.
  // Also reset the module-level panel-tab store (which holds inline-preview
  // drafts) so a draft from one test can't leak into the next.
  beforeEach(() => { localStorage.clear(); __resetPanelTabs() })

  it('renders each detected PR as a source selector in the Changes view', () => {
    render(
      <ActivityViewer
        {...baseProps}
        view="changes"
        sources={[
          { provider: 'github', owner: 'octo', repo: 'alpha', number: 42, url: 'https://github.com/octo/alpha/pull/42' },
          { provider: 'gitlab', owner: 'team', repo: 'beta', number: 7, url: 'https://gitlab.com/team/beta/-/merge_requests/7' },
        ]}
        selectedSourceUrl="https://github.com/octo/alpha/pull/42"
        onSelectSource={vi.fn()}
      />,
      { wrapper },
    )

    // Two distinct projects (octo/alpha, team/beta), so each tab is qualified
    // by its project path — a bare `PR #42` / `MR !7` would be ambiguous across
    // projects, and GitLab IIDs collide across projects in particular.
    expect(screen.getByRole('tab', { name: 'octo/alpha PR #42' })).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: 'team/beta MR !7' })).toBeInTheDocument()
    expect(screen.getByText('Loading source provider…')).toBeInTheDocument()
  })

  it('shows an empty state, not the Files view, when Changes is opened with no PR', () => {
    // Changes is a PINNED view (always present under `view` mode), so with no
    // sources it must NOT fall back to the touched-files list under a "Changes"
    // header — it owns its own PR empty state instead. Even with touched files
    // present, the Changes view stays empty.
    render(
      <ActivityViewer
        {...baseProps}
        view="changes"
        sources={[]}
        files={[{ path: '/proj/foo.ts', ts: 1, source: 'tool' }]}
      />,
      { wrapper },
    )
    expect(screen.queryByText('No files changed yet')).toBeNull()
    expect(screen.queryByText('/proj/foo.ts')).toBeNull()
    expect(screen.getByText(/No pull requests in this session yet/)).toBeInTheDocument()
  })

  it('Resources hides links present in the Changes tab (sources) and keeps the rest', () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const store = configureStore({
      reducer: { chat: chatReducer, dashboard: dashboardReducer, notifications: notificationsReducer },
    })
    store.dispatch(openActivityToTab('links'))
    const prUrl = 'https://github.com/kirodotdev/KiroCrew/pull/42'
    render(
      <Provider store={store}>
        <QueryClientProvider client={qc}>
          <ActivityViewer
            {...baseProps}
            // The Changes tab surfaces this PR, so it should NOT also appear in Resources.
            sources={[{ url: prUrl, provider: 'github', number: 42, repo: 'KiroCrew' }]}
            navLinks={[
              { url: prUrl, type: 'cr', label: 'PR #42', msgIdx: 0 },
              // Not in `sources` (a code-review host Changes can't render) — must stay reachable.
              { url: 'https://git.example.com/reviews/CR-1', type: 'cr', label: 'CR-1', msgIdx: 0 },
              { url: 'https://git.example.com/packages/KiroCrew', type: 'other', label: 'KiroCrew repo', msgIdx: 0 },
            ]}
          />
        </QueryClientProvider>
      </Provider>,
    )
    expect(screen.getByText('Resources')).toBeInTheDocument()
    // Non-Changes links stay in Resources.
    expect(screen.getByText('KiroCrew repo')).toBeInTheDocument()
    expect(screen.getByText('CR-1')).toBeInTheDocument()
    // The link already shown in the Changes tab is hidden from Resources.
    expect(screen.queryByText('PR #42')).not.toBeInTheDocument()
  })

  it('search filters links, with a no-matches state', async () => {
    render(
      <ActivityViewer
        {...baseProps}
        view="links"
        // >5 entries, so the search box clears its display threshold.
        navLinks={[
          { url: 'https://example.com/alpha', type: 'other', label: 'Alpha link', msgIdx: 0 },
          { url: 'https://example.com/beta', type: 'other', label: 'Beta link', msgIdx: 0 },
          { url: 'https://example.com/gamma', type: 'other', label: 'Gamma link', msgIdx: 0 },
          { url: 'https://example.com/delta', type: 'other', label: 'Delta link', msgIdx: 0 },
          { url: 'https://example.com/epsilon', type: 'other', label: 'Epsilon link', msgIdx: 0 },
          { url: 'https://example.com/zeta', type: 'other', label: 'Zeta link', msgIdx: 0 },
        ]}
      />,
      { wrapper },
    )
    expect(screen.getByText('Resources')).toBeInTheDocument()

    const box = screen.getByLabelText('Search by label or URL…')
    fireEvent.change(box, { target: { value: 'alpha' } })
    expect(screen.getByText('Alpha link')).toBeInTheDocument()
    expect(screen.queryByText('Beta link')).not.toBeInTheDocument()

    // A query matching nothing shows the no-matches state, not the empty state.
    fireEvent.change(box, { target: { value: 'zzz-no-such-thing' } })
    expect(screen.getByText('No matches')).toBeInTheDocument()
    expect(screen.queryByText('No links yet')).not.toBeInTheDocument()

    // Clearing restores everything.
    fireEvent.change(box, { target: { value: '' } })
    expect(screen.getByText('Beta link')).toBeInTheDocument()
  })

  it('keeps the search box mounted while a query is active, even below the threshold', async () => {
    render(
      <ActivityViewer
        {...baseProps}
        view="links"
        navLinks={[
          { url: 'https://example.com/alpha', type: 'other', label: 'Alpha link', msgIdx: 0 },
          { url: 'https://example.com/beta', type: 'other', label: 'Beta link', msgIdx: 0 },
          { url: 'https://example.com/gamma', type: 'other', label: 'Gamma link', msgIdx: 0 },
          { url: 'https://example.com/delta', type: 'other', label: 'Delta link', msgIdx: 0 },
          { url: 'https://example.com/epsilon', type: 'other', label: 'Epsilon link', msgIdx: 0 },
          { url: 'https://example.com/zeta', type: 'other', label: 'Zeta link', msgIdx: 0 },
        ]}
      />,
      { wrapper },
    )
    const label = 'Search by label or URL…'
    const box = screen.getByLabelText(label)
    // Filtering down to ONE match takes the visible count below the 5-entry
    // threshold. The box must NOT unmount — otherwise the stale query keeps
    // filtering with no input left to clear it, and the hidden rows read as lost.
    fireEvent.change(box, { target: { value: 'alpha' } })
    expect(screen.getByLabelText(label)).toBeInTheDocument()
    expect(screen.getByText('Alpha link')).toBeInTheDocument()
    expect(screen.queryByText('Beta link')).not.toBeInTheDocument()
    // Clearing brings everything back.
    fireEvent.change(screen.getByLabelText(label), { target: { value: '' } })
    expect(screen.getByText('Beta link')).toBeInTheDocument()
  })

  it('hides the search box for a short list (nothing to filter yet)', async () => {
    render(
      <ActivityViewer
        {...baseProps}
        view="links"
        navLinks={[{ url: 'https://example.com/alpha', type: 'other', label: 'Alpha link', msgIdx: 0 }]}
      />,
      { wrapper },
    )
    // The list renders, but a 1-item list is faster to scan than to filter.
    expect(screen.getByText('Alpha link')).toBeInTheDocument()
    expect(screen.queryByLabelText('Search by label or URL…')).not.toBeInTheDocument()
  })










  /* ── Files tab: add a file to the artifact library ─────────────────────────
   * The Artifacts tab lists artifact records only, so a plain file can no
   * longer reach the library by extension alone. The explicit way in lives on
   * the file row itself. */
})

// ── Artifacts tab: artifact records only ────────────────────────────────────
//
// SessionArtifactsTab lists real artifact RECORDS from two queries over the same
// store: the session's involvement scope (`?touched_by=`) and the whole library.
// Auto-registered widgets surface through the first — their HTML is inline in the
// message and never hits disk. It used to run a THIRD query for "session
// documents" (plain files admitted by extension), which put scratch notes in the
// library; these tests pin that those are gone, plus the de-dup and the
// save-permanently routing. SessionArtifactsTab uses useNavigate, so it needs a
// Router.
describe('ActivityViewer — subagent card layout', () => {
  // Layout: in the narrow activity rail, if every header item were shrinkable
  // "Subagent Running Tool", the elapsed clock and the Cancel button would all
  // wrap onto two lines and blow the card's header height up. The status label
  // is the last thing to give way (the agent chip yields first); the clock and
  // Cancel button must hold their single line.
  it('keeps the subagent card header on one line in a narrow rail', () => {
    const store = configureStore({
      reducer: { chat: chatReducer, dashboard: dashboardReducer, notifications: notificationsReducer },
    })
    store.dispatch(openActivityToTab('subagents'))
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <Provider store={store}>
        <QueryClientProvider client={qc}>
          <ActivityViewer
            toolLog={[]}
            open
            onToggle={vi.fn()}
            slot="test-slot"
            subagents={{
              s1: {
                id: 's1', task: 'READ-ONLY RESEARCH', agent: 'kirocrew', status: 'tool',
                streaming: '', lastTool: 'read', startedAt: Date.now() - 239_000, elapsed: 0,
              },
            }}
          />
        </QueryClientProvider>
      </Provider>,
    )

    // The status is the informative half, so it is what the header shows; the
    // full phrase stays reachable as the tooltip.
    const title = screen.getByText('Running Tool')
    expect(title).toHaveAttribute('title', 'Subagent Running Tool')
    expect(title.className).toContain('truncate')
    expect(title.className).toContain('min-w-0')

    // Agent chip: yields BEFORE the status label (weighted shrink) and capped,
    // so a long agent name can neither wrap nor starve the label, the clock and
    // the Cancel button.
    const chip = screen.getByText('kirocrew')
    expect(chip.className).toContain('shrink-[3]')
    expect(chip.className).toContain('truncate')
    expect(chip.className).toContain('min-w-0')

    const clock = screen.getByText('3m 59s')
    expect(clock.className).toContain('shrink-0')
    expect(clock.className).toContain('whitespace-nowrap')

    const cancel = screen.getByTestId('subagent-cancel-btn')
    expect(cancel.className).toContain('shrink-0')
    expect(cancel.className).toContain('whitespace-nowrap')
  })
})

/**
 * Queued-wave visibility. A spawn_run wave accepted but still behind the
 * concurrency cap / stagger gate emits `subagent_queued` and NOTHING else — no
 * per-agent entry exists yet. Rendering "No subagents running" for that entire
 * window is flatly false — the single most misleading state the panel can show.
 */
describe('ActivityViewer — queued subagents', () => {
  const SLOT = 'test-slot'
  const baseProps = { subagents: {}, toolLog: [], open: true, onToggle: vi.fn(), slot: SLOT }

  function queuedWrapper(queued: number, reason?: { reason: string; available_gb?: number; required_gb?: number }) {
    return function Wrapper({ children }: { children: React.ReactNode }) {
      const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
      const store = configureStore({
        reducer: { chat: chatReducer, dashboard: dashboardReducer, notifications: notificationsReducer },
      })
      if (queued > 0) store.dispatch(sseSubagentQueued({ slot: SLOT, queued, ...reason }))
      return (
        <Provider store={store}>
          <QueryClientProvider client={qc}>{children}</QueryClientProvider>
        </Provider>
      )
    }
  }

  it('announces agents waiting to start instead of claiming none are running', () => {
    render(<ActivityViewer {...baseProps} view="subagents" />, { wrapper: queuedWrapper(3) })
    expect(screen.getByTestId('subagent-queued-banner').textContent).toContain('3 waiting to start')
    expect(screen.queryByText('No subagents running')).not.toBeInTheDocument()
  })

  it('keeps the concurrency text for a count with no reason (older gateway)', () => {
    render(<ActivityViewer {...baseProps} view="subagents" />, { wrapper: queuedWrapper(3) })
    expect(screen.getByTestId('subagent-queued-banner').textContent)
      .toBe('3 waiting to start — queued behind the concurrency limit')
  })

  it('says a memory-deferred wave waits for memory, with both figures', () => {
    // The F20 report: cap 4, one queued, "queued behind the concurrency limit"
    // forever -- the memory guard had parked it and the panel never said so.
    render(<ActivityViewer {...baseProps} view="subagents" />, {
      wrapper: queuedWrapper(1, { reason: 'low_memory', available_gb: 3.2, required_gb: 4.5 }),
    })
    const text = screen.getByTestId('subagent-queued-banner').textContent ?? ''
    expect(text).toContain('1 waiting to start')
    expect(text).toMatch(/4\.5\s?GB/)
    expect(text).toMatch(/3\.2\s?GB/)
    expect(text).not.toContain('concurrency limit')
  })

  it('says a paused cap is about memory or load, in user words, not the concurrency limit', () => {
    render(<ActivityViewer {...baseProps} view="subagents" />, {
      wrapper: queuedWrapper(2, { reason: 'adaptive_cap_zero' }),
    })
    const text = screen.getByTestId('subagent-queued-banner').textContent ?? ''
    expect(text).toContain('low on memory or overloaded')
    expect(text).not.toContain('controller')
    expect(text).not.toContain('concurrency limit')
  })

  it('tells the user what to do about a memory wait', () => {
    render(<ActivityViewer {...baseProps} view="subagents" />, {
      wrapper: queuedWrapper(1, { reason: 'low_memory', available_gb: 3.2, required_gb: 4.5 }),
    })
    expect(screen.getByTestId('subagent-queued-banner').textContent).toContain('free up memory to continue')
  })

  it('drops to the figure-less sentence when a memory event carries no numbers', () => {
    render(<ActivityViewer {...baseProps} view="subagents" />, {
      wrapper: queuedWrapper(1, { reason: 'low_memory' }),
    })
    const text = screen.getByTestId('subagent-queued-banner').textContent ?? ''
    expect(text).toBe('1 waiting to start — not enough free memory; free up memory to continue')
    expect(text).not.toMatch(/needs .* of free memory/)
  })

  it('drops to the figure-less critical sentence when the posture event carries no number', () => {
    render(<ActivityViewer {...baseProps} view="subagents" />, {
      wrapper: queuedWrapper(1, { reason: 'posture_critical' }),
    })
    const text = screen.getByTestId('subagent-queued-banner').textContent ?? ''
    expect(text).toContain('critically low; free up memory')
    expect(text).not.toContain('(')
  })

  it('keeps the concurrency text for the concurrency kind itself', () => {
    render(<ActivityViewer {...baseProps} view="subagents" />, {
      wrapper: queuedWrapper(2, { reason: 'concurrency_limit' }),
    })
    expect(screen.getByTestId('subagent-queued-banner').textContent)
      .toBe('2 waiting to start — queued behind the concurrency limit')
  })

  it('keeps the honest empty state when nothing is queued or running', () => {
    render(<ActivityViewer {...baseProps} view="subagents" />, { wrapper: queuedWrapper(0) })
    expect(screen.getByText('No subagents running')).toBeInTheDocument()
    expect(screen.queryByTestId('subagent-queued-banner')).not.toBeInTheDocument()
  })
})

// ── Section headers are shared across tabs ──────────────────────────────────
//
// The Files and Artifacts tabs sit behind adjacent buttons in the same panel, so
// a user flipping between them sees both headers within a second of each other.
// They used to be hand-rolled separately and had drifted apart on case, size,
// weight, colour, divider, and whether the count was a node or punctuation in
// the label — one panel that looked like two. Both now route through
// PanelSectionHeader; these tests fail if either tab grows its own idiom again.
describe('ActivityViewer — panel section headers', () => {
  function panelWrapper({ children }: { children: React.ReactNode }) {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const store = configureStore({
      reducer: { chat: chatReducer, dashboard: dashboardReducer, notifications: notificationsReducer },
    })
    return (
      <Provider store={store}>
        <QueryClientProvider client={qc}>
          <MemoryRouter>{children}</MemoryRouter>
        </QueryClientProvider>
      </Provider>
    )
  }

  const headerProps = { subagents: {}, toolLog: [], open: true, onToggle: vi.fn(), slot: 'test-slot' }

  beforeEach(() => {
    vi.mocked(api.artifacts).mockResolvedValue({ artifacts: [] })
    vi.mocked(api.artifactSessionDocs).mockResolvedValue({ docs: [] })
  })

  it('renders the Links tab group through the shared header', () => {
    render(
      <ActivityViewer
        {...headerProps}
        view="links"
        navLinks={[{ url: 'https://example.com/x', type: 'other', label: 'Notes', msgIdx: 0 }]}
      />,
      { wrapper: panelWrapper },
    )
    expect(sectionHeaders()).toEqual([['Resources', '1']])
  })


})
