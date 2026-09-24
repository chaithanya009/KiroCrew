import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, fireEvent, waitFor } from '@testing-library/react'
import MarkdownRenderer from '../components/MarkdownRenderer'

const copied: string[] = []
let copyResult = true
vi.mock('../utils/clipboard', async importOriginal => {
  const actual = await importOriginal<typeof import('../utils/clipboard')>()
  return {
    ...actual,
    copyToClipboard: (text: string) => {
      copied.push(text)
      return Promise.resolve(copyResult)
    },
  }
})

/**
 * Blocked-link chip (rfc-redaction-explain-and-reveal §3).
 *
 * The redactor leaves `[REDACTED: suspicious URL to <domain>]` in the saved text
 * and records each blocked link in `meta.blocked_links`, keeping the full
 * address unless opening it would send a credential. Most blocked links are
 * false positives, so the chip gives the link back: See link shows the full
 * address with Open once and Copy link.
 */

const PH = (d: string) => `[REDACTED: suspicious URL to ${d}]`

interface Rec {
  domain: string
  rule: string
  path: string | null
  query_chars: number
  url: string | null
  url_withheld: 'credential' | 'length' | null
}
const rec = (over: Partial<Rec> = {}): Rec => {
  const domain = over.domain ?? 'example.com'
  return {
    domain,
    rule: 'exfil_query_length',
    path: '/p',
    query_chars: 9,
    url: `https://${domain}/p?q=abcdefghi`,
    url_withheld: null,
    ...over,
  }
}
const withheld = (over: Partial<Rec> = {}): Rec =>
  rec({ url: null, url_withheld: 'credential', ...over })

afterEach(() => {
  copied.length = 0
  copyResult = true
  vi.restoreAllMocks()
})

describe('blocked-link chip', () => {
  it('replaces the placeholder and leaves surrounding prose byte-identical', () => {
    const { container } = render(
      <MarkdownRenderer content={`Before ${PH('example.com')} after.`} blockedLinks={[rec()]} />,
    )
    expect(container.querySelector('[data-testid="blocked-link-chip"]')).not.toBeNull()
    const text = container.textContent ?? ''
    expect(text).not.toContain('[REDACTED')
    expect(text).toContain('Before ')
    expect(text).toContain(' after.')
  })

  it('renders one chip per placeholder in a message with several', () => {
    const { container } = render(
      <MarkdownRenderer
        content={`One ${PH('a.example.com')} two ${PH('b.example.com')} three`}
        blockedLinks={[rec({ domain: 'a.example.com' }), rec({ domain: 'b.example.com' })]}
      />,
    )
    expect(container.querySelectorAll('[data-testid="blocked-link-chip"]')).toHaveLength(2)
    expect(container.textContent ?? '').not.toContain('[REDACTED')
  })

  it('names what happened and says the link can be seen', () => {
    const { container, getByTestId } = render(
      <MarkdownRenderer content={PH('example.com')} blockedLinks={[rec()]} />,
    )
    const chip = container.querySelector('[data-testid="blocked-link-chip"]')!
    expect(chip.textContent).toContain('Link blocked')
    expect(getByTestId('blocked-link-inspect').textContent).toContain('See link')
  })

  it('shows the full address and opens it only on Open once, without a referrer', () => {
    const url = 'https://wiki.example.com/view/%E6%B5%8B%E8%AF%95?section=2&from=reply'
    const open = vi.spyOn(window, 'open').mockImplementation(() => null)
    const { getByTestId, queryByTestId } = render(
      <MarkdownRenderer
        content={PH('wiki.example.com')}
        blockedLinks={[rec({ domain: 'wiki.example.com', rule: 'exfil_percent_encoding', path: null, url })]}
      />,
    )
    expect(queryByTestId('blocked-link-inspect-panel')).toBeNull()
    fireEvent.click(getByTestId('blocked-link-inspect'))
    // Shown as the reader can recognise it; opened exactly as kept.
    expect(getByTestId('blocked-link-url').textContent).toBe('https://wiki.example.com/view/测试?section=2&from=reply')
    expect(open).not.toHaveBeenCalled()

    fireEvent.click(getByTestId('blocked-link-open-once'))
    expect(open).toHaveBeenCalledTimes(1)
    expect(open).toHaveBeenCalledWith(url, '_blank', 'noopener,noreferrer')
  })

  it('shows the escaped address when decoding would hide where it points', () => {
    // A right-to-left override can make decoded text read backwards on screen.
    const url = 'https://example.com/p/%E2%80%AEgpj.exe?q=abcdefghi'
    const { getByTestId } = render(<MarkdownRenderer content={PH('example.com')} blockedLinks={[rec({ url })]} />)
    fireEvent.click(getByTestId('blocked-link-inspect'))
    expect(getByTestId('blocked-link-url').textContent).toBe(url)
  })

  it('escapes a literal bidi character in the kept address and in the path line', () => {
    // Written literally, the override survives decoding unchanged, so the
    // raw string is no safe fallback: it must be shown escaped.
    const url = 'https://example.com/p/\u202Egpj.exe?q=abcdefghi'
    const { getByTestId, getByText } = render(
      <MarkdownRenderer content={PH('example.com')} blockedLinks={[rec({ url, path: '/p/\u202Egpj.exe' })]} />,
    )
    expect(getByText('example.com/p/%E2%80%AEgpj.exe')).toBeTruthy()
    fireEvent.click(getByTestId('blocked-link-inspect'))
    const shown = getByTestId('blocked-link-url').textContent ?? ''
    expect(shown).toBe('https://example.com/p/%E2%80%AEgpj.exe?q=abcdefghi')
    expect(/\p{C}/u.test(shown)).toBe(false)
  })

  it('copies the full address and says so', async () => {
    const { getByTestId } = render(<MarkdownRenderer content={PH('example.com')} blockedLinks={[rec()]} />)
    fireEvent.click(getByTestId('blocked-link-inspect'))
    fireEvent.click(getByTestId('blocked-link-copy'))
    expect(copied).toEqual(['https://example.com/p?q=abcdefghi'])
    await waitFor(() => expect(getByTestId('blocked-link-copy').textContent).toContain('Copied'))
  })


  it('a failed retry clears the earlier success', async () => {
    const { getByTestId } = render(<MarkdownRenderer content={PH('example.com')} blockedLinks={[rec()]} />)
    fireEvent.click(getByTestId('blocked-link-inspect'))
    fireEvent.click(getByTestId('blocked-link-copy'))
    await waitFor(() => expect(getByTestId('blocked-link-copy').textContent).toContain('Copied'))
    copyResult = false
    fireEvent.click(getByTestId('blocked-link-copy'))
    await waitFor(() => expect(getByTestId('blocked-link-copy-failed').textContent).toMatch(/Copy failed/))
    expect(getByTestId('blocked-link-copy').textContent).not.toContain('Copied')
  })
  it('says so when the clipboard refuses the copy', async () => {
    copyResult = false
    const { getByTestId, queryByTestId } = render(<MarkdownRenderer content={PH('example.com')} blockedLinks={[rec()]} />)
    fireEvent.click(getByTestId('blocked-link-inspect'))
    expect(queryByTestId('blocked-link-copy-failed')).toBeNull()
    fireEvent.click(getByTestId('blocked-link-copy'))
    await waitFor(() => expect(getByTestId('blocked-link-copy-failed').textContent).toMatch(/Copy failed/))
    expect(getByTestId('blocked-link-copy').textContent).not.toContain('Copied')
  })

  it('remembers nothing: a fresh render is blocked and collapsed again', () => {
    vi.spyOn(window, 'open').mockImplementation(() => null)
    const first = render(<MarkdownRenderer content={PH('example.com')} blockedLinks={[rec()]} />)
    fireEvent.click(first.getByTestId('blocked-link-inspect'))
    fireEvent.click(first.getByTestId('blocked-link-open-once'))
    first.unmount()

    const again = render(<MarkdownRenderer content={PH('example.com')} blockedLinks={[rec()]} />)
    expect(again.queryByTestId('blocked-link-chip')).not.toBeNull()
    expect(again.queryByTestId('blocked-link-inspect-panel')).toBeNull()
  })

  it('offers no Open for an address that would send a credential, and says why', () => {
    const { getByTestId, queryByTestId } = render(
      <MarkdownRenderer
        content={PH('example.com')}
        blockedLinks={[withheld({ rule: 'exfil_hard_credential', query_chars: 0 })]}
      />,
    )
    expect(getByTestId('blocked-link-inspect').textContent).toContain('Why removed?')
    // Collapsed, the chip already says the address is gone rather than gated.
    expect(getByTestId('blocked-link-chip').textContent).toContain('Link removed')
    expect(getByTestId('blocked-link-chip').textContent).not.toContain('Link blocked')
    fireEvent.click(getByTestId('blocked-link-inspect'))
    expect(queryByTestId('blocked-link-open-once')).toBeNull()
    expect(queryByTestId('blocked-link-copy')).toBeNull()
    expect(queryByTestId('blocked-link-url')).toBeNull()
    expect(getByTestId('blocked-link-withheld').textContent).toMatch(/would send that secret/)
    expect(getByTestId('blocked-link-inspect-panel').textContent).toContain('exfil_hard_credential')
  })

  it('says a too-long address was not kept rather than calling it a secret', () => {
    const { getByTestId } = render(
      <MarkdownRenderer content={PH('example.com')} blockedLinks={[withheld({ url_withheld: 'length' })]} />,
    )
    fireEvent.click(getByTestId('blocked-link-inspect'))
    expect(getByTestId('blocked-link-withheld').textContent).toMatch(/too long/)
  })

  it('counts the query as hidden when it is kept, and removed when it is not', () => {
    const kept = render(<MarkdownRenderer content={PH('example.com')} blockedLinks={[rec({ query_chars: 290 })]} />)
    expect(kept.getByTestId('blocked-link-query').textContent).toBe('290 more characters after the address')
    kept.unmount()
    const gone = render(<MarkdownRenderer content={PH('example.com')} blockedLinks={[withheld({ query_chars: 290 })]} />)
    expect(gone.getByTestId('blocked-link-query').textContent).toBe('290 characters removed after the address')
  })

  it('shows no query line when there is no query', () => {
    const { container } = render(
      <MarkdownRenderer content={PH('example.com')} blockedLinks={[rec({ query_chars: 0, url: 'https://example.com/p' })]} />,
    )
    expect(container.querySelector('[data-testid="blocked-link-query"]')).toBeNull()
  })

  it('collapses byte-identical records into one and shows its path', () => {
    const r = rec({ path: '/api/v1', url: 'https://example.com/api/v1?q=abcdefghi' })
    const { getByTestId } = render(<MarkdownRenderer content={PH('example.com')} blockedLinks={[r, { ...r }]} />)
    expect(getByTestId('blocked-link-target').textContent).toBe('example.com/api/v1')
    fireEvent.click(getByTestId('blocked-link-inspect'))
    expect(document.querySelectorAll('[data-testid="blocked-link-entry"]')).toHaveLength(1)
  })

  it('lists every distinct link on one site, each openable, instead of guessing which is which', () => {
    const open = vi.spyOn(window, 'open').mockImplementation(() => null)
    const a = rec({ path: '/first', url: 'https://example.com/first?q=1111' })
    const b = rec({ path: '/second', rule: 'exfil_query_pattern', url: 'https://example.com/second?q=2222' })
    const { getAllByTestId } = render(
      <MarkdownRenderer content={`${PH('example.com')} and ${PH('example.com')}`} blockedLinks={[a, b]} />,
    )
    const inspect = getAllByTestId('blocked-link-inspect')[0]
    expect(inspect.textContent).toContain('See links')
    expect(getAllByTestId('blocked-link-target')[0].textContent).toBe('example.com')
    expect(getAllByTestId('blocked-link-many')[0].textContent).toBe('2 blocked links on this site, all listed inside')
    fireEvent.click(inspect)
    const panel = getAllByTestId('blocked-link-inspect-panel')[0]
    const urls = [...panel.querySelectorAll('[data-testid="blocked-link-url"]')].map(n => n.textContent)
    expect(urls).toEqual([a.url, b.url])
    // Each entry names its own rule.
    expect(panel.textContent).toContain('exfil_query_length')
    expect(panel.textContent).toContain('exfil_query_pattern')
    fireEvent.click(panel.querySelectorAll('[data-testid="blocked-link-open-once"]')[1])
    expect(open).toHaveBeenCalledWith(b.url, '_blank', 'noopener,noreferrer')
  })

  it('is never an anchor, and every control inside it is a button', () => {
    const { container, getByTestId } = render(<MarkdownRenderer content={PH('example.com')} blockedLinks={[rec()]} />)
    fireEvent.click(getByTestId('blocked-link-inspect'))
    const chip = container.querySelector('[data-testid="blocked-link-chip"]')!
    expect(chip.tagName.toLowerCase()).not.toBe('a')
    expect(chip.querySelector('a')).toBeNull()
    for (const id of ['blocked-link-inspect', 'blocked-link-open-once', 'blocked-link-copy']) {
      expect(getByTestId(id).tagName.toLowerCase(), id).toBe('button')
    }
  })

  it('does not dress the disclosure as a link at rest', () => {
    // This app's anchor style is accent text with an underline.
    const { getByTestId } = render(<MarkdownRenderer content={PH('example.com')} blockedLinks={[rec()]} />)
    const cls = getByTestId('blocked-link-inspect').className
    expect(cls).not.toMatch(/(^|\s)text-accent(\s|$)/)
    expect(cls).not.toContain('underline')
    expect(cls).toContain('hover:text-accent')
  })

  it('drops a record whose address could open somewhere the chip does not name', () => {
    for (const url of [
      'https://attacker.example.net/?q=1',
      'javascript:alert(1)',
      'https://example.com@attacker.example.net/',
    ]) {
      const { container, unmount } = render(
        <MarkdownRenderer content={PH('example.com')} blockedLinks={[rec({ url })]} />,
      )
      expect(container.querySelector('[data-testid="blocked-link-chip"]'), url).toBeNull()
      expect(container.textContent).toContain(PH('example.com'))
      unmount()
    }
  })

  it('drops a record that carries both halves, or neither', () => {
    for (const bad of [rec({ url_withheld: 'length' }), rec({ url: null, url_withheld: null })]) {
      const { container, unmount } = render(<MarkdownRenderer content={PH('example.com')} blockedLinks={[bad]} />)
      expect(container.querySelector('[data-testid="blocked-link-chip"]')).toBeNull()
      unmount()
    }
  })

  it('gives every rule the redactor can emit its own reason, never the generic one', () => {
    const expected: Record<string, RegExp> = {
      exfil_query_length: /characters after the address/i,
      exfil_query_pattern: /characters after the address/i,
      exfil_percent_encoding: /escaped characters/i,
      exfil_decode_saturated: /escaped characters/i,
      exfil_encoded_credential: /secret or credential/i,
      exfil_fixed_credential: /secret or credential/i,
      exfil_hard_credential: /secret or credential/i,
    }
    for (const [rule, pattern] of Object.entries(expected)) {
      const { getByTestId, unmount } = render(
        <MarkdownRenderer content={PH('example.com')} blockedLinks={[withheld({ rule, path: null })]} />,
      )
      fireEvent.click(getByTestId('blocked-link-inspect'))
      expect(getByTestId('blocked-link-inspect-panel').textContent, rule).toMatch(pattern)
      unmount()
    }
  })

  it('tells the two query rules apart on screen', () => {
    const panelText = (rule: string) => {
      const { container, unmount } = render(
        <MarkdownRenderer content={PH('metrics.example.com')} blockedLinks={[rec({ domain: 'metrics.example.com', rule })]} />,
      )
      fireEvent.click(container.querySelector('[data-testid="blocked-link-inspect"]')!)
      const text = container.querySelector('[data-testid="blocked-link-inspect-panel"]')?.textContent ?? ''
      unmount()
      return text
    }
    expect(panelText('exfil_query_length')).not.toEqual(panelText('exfil_query_pattern'))
  })

  it('leaves the placeholder as plain text when no record matches its domain', () => {
    const { container } = render(
      <MarkdownRenderer content={PH('unlisted.example.com')} blockedLinks={[rec({ domain: 'other.example.com' })]} />,
    )
    expect(container.querySelector('[data-testid="blocked-link-chip"]')).toBeNull()
    expect(container.textContent).toContain('[REDACTED: suspicious URL to unlisted.example.com]')
  })

  it('explains and opens every host shape the backend retains', () => {
    vi.spyOn(window, 'open').mockImplementation(() => null)
    for (const host of ['my_service.example.com', '203.0.113.7', '[2001:db8::1]']) {
      const url = `https://${host}/c?q=zzzzzzzzz`
      const { container, getByTestId, unmount } = render(
        <MarkdownRenderer content={PH(host)} blockedLinks={[rec({ domain: host, path: '/c', url })]} />,
      )
      const chip = container.querySelector('[data-testid="blocked-link-chip"]')
      expect(chip, host).not.toBeNull()
      expect(chip!.textContent).toContain(host)
      expect(container.textContent).not.toContain('[REDACTED')
      fireEvent.click(getByTestId('blocked-link-inspect'))
      expect(getByTestId('blocked-link-url').textContent, host).toBe(url)
      unmount()
    }
  })

  it('leaves a placeholder inside an anchor as plain text', () => {
    // A control inside an anchor navigates: the click would follow the anchor's
    // own agent-authored destination, which is the thing under suspicion.
    const { container } = render(
      <MarkdownRenderer
        content={`<a href="https://elsewhere.example/">${PH('reports.example.com')}</a>`}
        blockedLinks={[rec({ domain: 'reports.example.com' })]}
      />,
    )
    expect(container.querySelector('[data-testid="blocked-link-chip"]')).toBeNull()
    expect(container.querySelector('button')).toBeNull()
    expect(container.textContent).toContain('[REDACTED: suspicious URL to reports.example.com]')
  })

  it('does not offer the chip body as a click target', () => {
    const { container } = render(<MarkdownRenderer content={PH('example.com')} blockedLinks={[rec()]} />)
    const chip = container.querySelector('[data-testid="blocked-link-chip"]')!
    expect(chip.className).toContain('cursor-default')
    expect(container.querySelector('[data-testid="blocked-link-inspect"]')!.className).toContain('cursor-pointer')
  })

  it('centres the letters of each text run rather than their line boxes', () => {
    const { container } = render(<MarkdownRenderer content={PH('example.com')} blockedLinks={[rec({ query_chars: 12 })]} />)
    const chip = container.querySelector('[data-testid="blocked-link-chip"]')!
    const label = [...chip.querySelectorAll('span')].find(s => s.textContent?.trim() === 'Link blocked')!
    for (const el of [label, container.querySelector('[data-testid="blocked-link-target"]')!, container.querySelector('[data-testid="blocked-link-query"]')!]) {
      expect(el.className).toContain('[text-box:trim-both_cap_alphabetic]')
    }
  })
})
