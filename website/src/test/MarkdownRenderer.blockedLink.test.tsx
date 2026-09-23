import { describe, it, expect } from 'vitest'
import { render, fireEvent } from '@testing-library/react'
import MarkdownRenderer from '../components/MarkdownRenderer'

/**
 * Blocked-link chip (rfc-redaction-explain-and-reveal §3, rollout step 3).
 *
 * `redact_exfiltration_urls` leaves `[REDACTED: suspicious URL to <domain>]` in
 * the saved transcript and attaches per-URL STRUCTURE records to the message's
 * `meta.blocked_links` (the query VALUE is never kept). The chip renders from
 * those records, paired BY DOMAIN — the placeholder carries only the domain, so
 * a positional index could cross one link's path onto another.
 */

const PH = (d: string) => `[REDACTED: suspicious URL to ${d}]`

describe('blocked-link chip', () => {
  it('replaces the placeholder and leaves surrounding prose byte-identical', () => {
    const { container } = render(
      <MarkdownRenderer
        content={`Before ${PH('example.com')} after.`}
        blockedLinks={[{ domain: 'example.com', rule: 'exfil_query_pattern', path: '/p', query_chars: 0 }]}
      />,
    )
    const chip = container.querySelector('[data-testid="blocked-link-chip"]')
    expect(chip).not.toBeNull()
    // The placeholder is gone, replaced by the chip.
    const text = container.textContent ?? ''
    expect(text).not.toContain('[REDACTED')
    // Prose on both sides survives exactly, including its single spaces.
    expect(text).toContain('Before ')
    expect(text).toContain(' after.')
  })

  it('renders one chip per placeholder in a message with several', () => {
    const { container } = render(
      <MarkdownRenderer
        content={`One ${PH('a.example')} two ${PH('b.example')} three`}
        blockedLinks={[
          { domain: 'a.example', rule: 'exfil_unknown', path: null, query_chars: 0 },
          { domain: 'b.example', rule: 'exfil_unknown', path: null, query_chars: 0 },
        ]}
      />,
    )
    expect(container.querySelectorAll('[data-testid="blocked-link-chip"]')).toHaveLength(2)
    expect((container.textContent ?? '')).not.toContain('[REDACTED')
  })

  it('shows full detail for one record, or several byte-identical ones', () => {
    const rec = { domain: 'example.com', rule: 'exfil_query_length', path: '/api/v1', query_chars: 12 }
    const { container } = render(
      <MarkdownRenderer content={PH('example.com')} blockedLinks={[rec, { ...rec }]} />,
    )
    // Byte-identical duplicates collapse to one record → full detail: path shown.
    expect(container.querySelector('[data-testid="blocked-link-target"]')!.textContent).toBe('example.com/api/v1')
    expect(container.querySelector('[data-testid="blocked-link-query"]')!.textContent).toContain('12')
  })

  it('degrades to domain and reason only for several DIFFERING records on one domain', () => {
    const { container, getByTestId } = render(
      <MarkdownRenderer
        content={PH('example.com')}
        blockedLinks={[
          { domain: 'example.com', rule: 'exfil_query_pattern', path: '/first', query_chars: 8 },
          { domain: 'example.com', rule: 'exfil_query_pattern', path: '/second', query_chars: 3 },
        ]}
      />,
    )
    // No path from either record is attached (index pairing is the defect class
    // this avoids), and no query-chars line.
    expect(container.querySelector('[data-testid="blocked-link-target"]')!.textContent).toBe('example.com')
    expect(container.querySelector('[data-testid="blocked-link-query"]')).toBeNull()
    // Reason is still shown on Inspect.
    fireEvent.click(getByTestId('blocked-link-inspect'))
    expect(getByTestId('blocked-link-inspect-panel').textContent).toBeTruthy()
  })

  it('omits the path when the record keeps none (path === null)', () => {
    const { container } = render(
      <MarkdownRenderer
        content={PH('example.com')}
        blockedLinks={[{ domain: 'example.com', rule: 'exfil_unknown', path: null, query_chars: 0 }]}
      />,
    )
    expect(container.querySelector('[data-testid="blocked-link-target"]')!.textContent).toBe('example.com')
  })

  it('shows "query N chars" only when query_chars is positive', () => {
    const zero = render(
      <MarkdownRenderer
        content={PH('z.example')}
        blockedLinks={[{ domain: 'z.example', rule: 'exfil_unknown', path: null, query_chars: 0 }]}
      />,
    )
    expect(zero.container.querySelector('[data-testid="blocked-link-query"]')).toBeNull()

    const pos = render(
      <MarkdownRenderer
        content={PH('p.example')}
        blockedLinks={[{ domain: 'p.example', rule: 'exfil_unknown', path: null, query_chars: 7 }]}
      />,
    )
    expect(pos.container.querySelector('[data-testid="blocked-link-query"]')!.textContent).toContain('7')
  })

  it('is never an anchor and never navigates', () => {
    const { container } = render(
      <MarkdownRenderer
        content={PH('example.com')}
        blockedLinks={[{ domain: 'example.com', rule: 'exfil_query_pattern', path: '/p', query_chars: 4 }]}
      />,
    )
    const chip = container.querySelector('[data-testid="blocked-link-chip"]')!
    expect(chip.tagName.toLowerCase()).not.toBe('a')
    expect(chip.querySelector('a')).toBeNull()
    // The disclosure is a button, not a link.
    const inspect = container.querySelector('[data-testid="blocked-link-inspect"]')!
    expect(inspect.tagName.toLowerCase()).toBe('button')
  })

  it('Inspect states plainly that the query was not kept', () => {
    const { getByTestId, queryByTestId } = render(
      <MarkdownRenderer
        content={PH('example.com')}
        blockedLinks={[{ domain: 'example.com', rule: 'exfil_query_pattern', path: '/p', query_chars: 9 }]}
      />,
    )
    // Collapsed by default.
    expect(queryByTestId('blocked-link-inspect-panel')).toBeNull()
    // The control names the outcome it delivers. "Inspect" would promise a look
    // at the link, and the one thing a reader wants — the query — is the thing
    // deliberately not kept, so that label sends them hunting for something the
    // panel then denies.
    expect(getByTestId('blocked-link-inspect').textContent).toContain('Why removed?')
    // This app's anchor style IS accent text with an underline, and the chip is
    // deliberately not a link, so the disclosure must not borrow either at rest.
    const cls = getByTestId('blocked-link-inspect').className
    expect(cls).not.toMatch(/(^|\s)text-accent(\s|$)/)
    expect(cls).not.toContain('underline')
    expect(cls).toContain('hover:text-accent')
    fireEvent.click(getByTestId('blocked-link-inspect'))
    const panel = getByTestId('blocked-link-inspect-panel')
    expect(panel.textContent).toContain('not kept')
    // The exact rule id is disclosed, and the query VALUE never is.
    expect(panel.textContent).toContain('exfil_query_pattern')
  })

  it('gives every rule the redactor can emit its own reason, never the generic one', () => {
    // The ids come from `trace()` in security/exfil.py. A rule that falls through
    // to the generic sentence is a chip that explains nothing, and the encoding
    // rules must NOT borrow the credential sentence: a long run of escaped
    // characters is also what an ordinary non-Latin title turns into.
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
        <MarkdownRenderer
          content={PH('example.com')}
          blockedLinks={[{ domain: 'example.com', rule, path: null, query_chars: 300 }]}
        />,
      )
      fireEvent.click(getByTestId('blocked-link-inspect'))
      expect(getByTestId('blocked-link-inspect-panel').textContent, rule).toMatch(pattern)
      unmount()
    }
  })

  it('leaves the placeholder as plain text when no record matches its domain', () => {
    const { container } = render(
      <MarkdownRenderer
        content={PH('unlisted.example')}
        blockedLinks={[{ domain: 'other.example', rule: 'exfil_unknown', path: null, query_chars: 0 }]}
      />,
    )
    expect(container.querySelector('[data-testid="blocked-link-chip"]')).toBeNull()
    expect(container.textContent).toContain('[REDACTED: suspicious URL to unlisted.example]')
  })

  it('names the removal, not a block the reader could undo', () => {
    // The media gate says "blocked" for something held back and loadable. One
    // word for two finalities sends the reader hunting this chip for a reveal
    // that does not exist, so this chip states what actually happened.
    const { container } = render(
      <MarkdownRenderer
        content={PH('reports.example.com')}
        blockedLinks={[{ domain: 'reports.example.com', rule: 'exfil_query_length', path: null, query_chars: 9 }]}
      />,
    )

    const chip = container.querySelector('[data-testid="blocked-link-chip"]')
    expect(chip!.textContent).toContain('Link removed')
    expect(chip!.textContent).not.toContain('click to load')
  })

  it('names no rule id when the records for one host disagree', () => {
    // An id the redactor never emits is worse than no id: an owner searching for
    // it finds nothing, which is the id line's one job failed exactly where the
    // records conflict.
    const { container, getByTestId } = render(
      <MarkdownRenderer
        content={PH('cdn.example.io')}
        blockedLinks={[
          { domain: 'cdn.example.io', rule: 'exfil_query_length', path: '/a', query_chars: 260 },
          { domain: 'cdn.example.io', rule: 'exfil_hard_credential', path: '/b', query_chars: 980 },
        ]}
      />,
    )
    fireEvent.click(getByTestId('blocked-link-inspect'))

    const panel = getByTestId('blocked-link-inspect-panel')
    expect(panel.textContent).toContain('It matched a rule for links that can send conversation data out.')
    expect(panel.textContent).not.toContain('Rule id')
    expect(container.textContent).not.toContain('exfil_unknown')
  })

  it('tells the two query rules apart on screen', () => {
    // Identical sentences under different ids leave the reader unable to see
    // what differed. Length is about how much was there; pattern is about what
    // it looked like.
    const open = (rule: string) => {
      const { container, unmount } = render(
        <MarkdownRenderer
          content={PH('metrics.example.com')}
          blockedLinks={[{ domain: 'metrics.example.com', rule, path: '/p', query_chars: 200 }]}
        />,
      )
      fireEvent.click(container.querySelector('[data-testid="blocked-link-inspect"]')!)
      const text = container.querySelector('[data-testid="blocked-link-inspect-panel"]')?.textContent ?? ''
      unmount()
      return text
    }

    expect(open('exfil_query_length')).not.toEqual(open('exfil_query_pattern'))
  })

  it('explains every host shape the backend retains', () => {
    // The render side must not DROP a host the backend accepts: a dropped record
    // leaves the placeholder in the text with nothing explaining it, and the
    // shapes most likely to matter are the ones least likely to be typed by
    // hand. Underscores are real in internal and SRV names.
    for (const host of ['my_service.example.com', '203.0.113.7', '[2001:db8::1]']) {
      const { container, unmount } = render(
        <MarkdownRenderer
          content={PH(host)}
          blockedLinks={[{ domain: host, rule: 'exfil_query_length', path: '/c', query_chars: 250 }]}
        />,
      )

      const chip = container.querySelector('[data-testid="blocked-link-chip"]')
      expect(chip, host).not.toBeNull()
      expect(chip!.textContent).toContain(host)
      expect(container.textContent).not.toContain('[REDACTED')
      unmount()
    }
  })

  it('explains a bracketed IPv6 host like any other', () => {
    // The backend spells an IPv6 host with its brackets, so the placeholder
    // carries them. A capture that stopped at the inner bracket left the reader
    // a mangled placeholder and no chip.
    const { container } = render(
      <MarkdownRenderer
        content={PH('[2001:db8::1]')}
        blockedLinks={[{ domain: '[2001:db8::1]', rule: 'exfil_query_length', path: '/reports', query_chars: 250 }]}
      />,
    )

    const chip = container.querySelector('[data-testid="blocked-link-chip"]')
    expect(chip).not.toBeNull()
    expect(chip!.textContent).toContain('[2001:db8::1]')
    expect(container.textContent).not.toContain('[REDACTED')
  })

  it('leaves a placeholder inside an anchor as plain text', () => {
    // The chip carries a control, and a control inside an anchor navigates: the
    // disclosure click would follow the anchor's own agent-authored destination,
    // which is the thing under suspicion. Plain text explains nothing but takes
    // the reader nowhere, which is the safe half of that trade.
    const { container } = render(
      <MarkdownRenderer
        content={`<a href="https://elsewhere.example/">${PH('reports.example.com')}</a>`}
        blockedLinks={[{ domain: 'reports.example.com', rule: 'exfil_query_length', path: null, query_chars: 9 }]}
      />,
    )

    expect(container.querySelector('[data-testid="blocked-link-chip"]')).toBeNull()
    expect(container.querySelector('button')).toBeNull()
    expect(container.textContent).toContain('[REDACTED: suspicious URL to reports.example.com]')
  })

  it('does not offer the chip body as a click target', () => {
    const { container } = render(
      <MarkdownRenderer
        content={PH('reports.example.com')}
        blockedLinks={[{ domain: 'reports.example.com', rule: 'exfil_query_length', path: null, query_chars: 9 }]}
      />,
    )

    const chip = container.querySelector('[data-testid="blocked-link-chip"]')
    expect(chip).not.toBeNull()
    expect(chip!.className).toContain('cursor-default')
    // The one interactive thing inside it still reads as interactive.
    const btn = container.querySelector('[data-testid="blocked-link-inspect"]')
    expect(btn!.className).toContain('cursor-pointer')
  })
})
